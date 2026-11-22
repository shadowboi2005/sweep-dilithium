#!/usr/bin/env python3
"""Step 1: find instruction-skip faults with no IR-fault information.

For every function in the config (or --functions), every machine instruction
in its symbol range is skipped once per trial (first execution, hit=1) and
scored with the IR-level run's three tests on the IR-level run's inputs:

  * inputs, secret sweep and grid exactly as QEMU-test builds them
    (qemu_inputs.py): public seeds p (--seeds 0 1 2 3), ephemeral draws r
    (--eph-draws 4, for functions with an eph_secret), secret values
    domain_values(dist, --field-mod 256) at --secret-pos 0;
  * QEMU-test's early-stop sweep: secret values in order, stop as soon as both
    ineffective and correction have a witness; a crashed trial skips that
    secret value, a trial past --trial-timeout aborts the instruction
    (verdict "timeout");
  * direct_leak on up to 16 trace pairs of that sweep (detectors.py).

The unfaulted ("correct") runs depend only on (s, p, r), so they are run once
per function and shared by all of its instructions.

Outputs (--out DIR):
  machine_faults.csv   one row per (function, pc)
  timing_dilithium.csv QEMU-test's function,status,seconds format (+ columns)
  layouts.json         per function: argument layout, active lengths, sweep setup
  checkpoint.jsonl     finished instructions; rerunning the command resumes

  python src/find_faults.py --elf elf/dil2_O0.elf --ir elf/dil2.relabel.ll --out out/O0
"""
import argparse
import csv
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import capstone  # noqa: E402
import numpy as np  # noqa: E402
import unicorn  # noqa: E402

import detectors as T  # noqa: E402
import emu  # noqa: E402
import ir  # noqa: E402
import qemu_inputs as QI  # noqa: E402
from emu import EmulationError, EmulationTimeout, Machine, Skip  # noqa: E402

_ALL = {}  # function -> context; filled in the parent before the pools fork
_CTX = {}  # the context of the task a worker is running
_W = {}  # per-process harnesses, one per function


# --- per-process state -----------------------------------------------------------

def _use(fn):
    global _CTX
    _CTX = _ALL[fn]
    if _W.get("pid") != os.getpid():
        _W.clear()
        _W["pid"] = os.getpid()
    h = _W.get(fn)
    if h is None:
        m = Machine(_CTX["elf"])
        h = _W[fn] = QI.Harness(m, _CTX["module"], fn, _CTX["layout"], _CTX["fixed"],
                                _CTX["eph"], _CTX["trial_timeout"])
    return h


def _words(rec):
    c = _CTX
    return T.decode_words(T.get_buffer(rec, c["out_buf"]), c["word_size"])[:c["n_words"]]


# --- unfaulted runs ----------------------------------------------------------------

def _golden(task):
    fn, idx, p, j = task
    h = _use(fn)
    c = _CTX
    w = h.inputs("collect", p, c["bg_field_mod"], c["active"], (c["secret"], c["secret_pos"], c["sweep"][idx]),
                 f"eph:{j}")
    t0, c0 = time.perf_counter(), time.thread_time()
    try:
        rec = h.run(w, restore=True)
        out = ("ok", rec)
    except EmulationTimeout as e:
        out = ("timeout", str(e))
    except EmulationError as e:
        out = ("fail", str(e))
    return task, out, time.perf_counter() - t0, time.thread_time() - c0


# --- one instruction ----------------------------------------------------------------

def _dir(p, j):
    """The (p, r) trial directory name QEMU-test would use (for trace order)."""
    s = "" if p == _CTX["seeds"][0] else f"seed{p}"
    if j:
        s = f"{s}/eph{j}" if s else f"eph{j}"
    return s


def _pc(task):
    fn, pc, asm = task
    h = _use(fn)
    skip = Skip(h.m, pc, _CTX["it_blocks"].get(pc))
    try:
        return _sweep_pc(h, skip, pc, asm)
    finally:
        skip.detach()


def _sweep_pc(h, skip, pc, asm):
    c, m = _CTX, h.m
    t0, c0, i0 = time.perf_counter(), time.thread_time(), m.icount
    sweep = T.EarlyStopSweep(c["seeds"], c["n_eph"], c["n_words"], c["diff_mode"])
    dirs = defaultdict(list)
    trials = fired = crash = sv_seen = sv_skipped = 0
    timeout = None
    for idx, sv in enumerate(c["sweep"]):
        words, failed = {}, False
        for p, j in c["grid"]:
            st, g = c["golden"][(idx, p, j)]
            if st == "timeout":
                timeout = f"correct run: {g}"
                break
            if st == "fail":
                failed = True
                break
            skip.reset()
            trials += 1
            try:
                f = h.run(g["inputs"], restore=True)
            except EmulationTimeout as e:
                timeout = str(e)
                break
            except EmulationError:
                crash += 1
                failed = True
                break
            finally:
                fired += skip.fired > 0
            dirs[_dir(p, j)].append((idx, g, f))
            words[(p, j)] = (_words(g), _words(f))
        if timeout:
            break
        if failed:
            sv_skipped += 1
            continue
        sv_seen += 1
        if sweep.feed(sv, words):
            break
    if timeout:
        ine = cor = ("timeout", "subprocess.TimeoutExpired")
    else:
        ine, cor = sweep.verdicts()
    pairs = T.trace_pairs(dirs, c["traces"])
    dl = None
    if not pairs:
        if timeout:
            dl = ("timeout", "subprocess.TimeoutExpired: no trace pairs and the fault hangs")
        else:
            pairs, dl = _fallback_pairs(h, skip)
    if dl is None:
        dl = T.direct_leak(c["secret"], c["eph"], pairs)
    return {"function": c["fn"], "pc": pc, "asm": asm, "fired": fired, "trials": trials,
            "sv_examined": sv_seen, "sv_skipped": sv_skipped, "crash": crash, "timeout": int(bool(timeout)),
            "ineffective": ine[0], "ineffective_detail": ine[1],
            "correction": cor[0], "correction_detail": cor[1],
            "direct_leak": dl[0], "direct_leak_detail": dl[1], "n_trace_pairs": len(pairs),
            "seconds": round(time.perf_counter() - t0, 3), "cpu_seconds": round(time.thread_time() - c0, 3),
            "emu_insns": m.icount - i0}


FALLBACK_PAIRS = 4  # direct_leak.py


def _fallback_pairs(h, skip):
    """direct_leak.py collect_pairs(): seed k, eph draw k, no secret override."""
    c, pairs = _CTX, []
    for k in range(FALLBACK_PAIRS):
        w = h.inputs("collect", k, c["bg_field_mod"], c["active"], None, f"eph:{k}")
        try:
            skip.disarm()
            g = h.run(w, restore=True)
            skip.reset()
            f = h.run(w, restore=True)
        except EmulationTimeout:
            return pairs, ("timeout", "subprocess.TimeoutExpired")
        except EmulationError as e:
            return pairs, ("error", f"trace collection failed: {e}")
        pairs.append((g, f))
    return pairs, None


# --- driver --------------------------------------------------------------------------

FIELDS = ["function", "pc", "asm", "fired", "trials", "sv_examined", "sv_skipped", "crash", "timeout",
          "ineffective", "ineffective_detail", "correction", "correction_detail",
          "direct_leak", "direct_leak_detail", "n_trace_pairs", "seconds", "cpu_seconds", "emu_insns"]


def setup_function(a, module, cfg, fn):
    """Layout, calibration and sweep parameters, as QEMU-test derives them."""
    entry = cfg[fn]
    secret = entry["secret-buf"]
    fixed = [s for s in (entry.get("fixed-scalars") or "").split(",") if s]
    eph = [s for s in (entry.get("eph_secret") or "").split(",") if s]
    layout = QI.load_witness(a.witness_dir, fn)
    source = "witness"
    if layout is None:
        layout = QI.IRLayout(module).build(fn, QI.load_sample(a.inputs, fn))
        source = "rebuilt from IR + function_inputs"
    if secret not in layout:
        raise ValueError(f"secret-buf {secret!r} not in layout {list(layout)}")
    for e in eph:
        if e not in layout:
            raise ValueError(f"eph_secret {e!r} not in layout")
    m = Machine(a.elf)
    h = QI.Harness(m, module, fn, layout, fixed, eph, a.trial_timeout)
    t0, c0 = time.perf_counter(), time.thread_time()
    active = QI.load_active_lengths(a.witness_dir, fn)
    cal_failed = {}
    if active is None:
        active, cal_failed = QI.calibrate(h, a.field_mod)
    t_cal = (time.perf_counter() - t0, time.thread_time() - c0)
    n_active = active.get(secret)  # None: not calibrated, the whole buffer is filled
    if n_active is not None and n_active <= a.secret_pos and layout[secret].get("type") != "scalar":
        raise ValueError(f"secret-buf {secret!r} has active length {n_active}: "
                         "no secret value would ever reach the function")
    out_buf, active_len, ws, dm, _ = QI.derive_all(layout, secret, a.field_mod)
    sdist = layout[secret].get("distribution")
    sweep = QI.domain_values(sdist, a.field_mod)
    n_eph = a.eph_draws if eph else 1
    return dict(fn=fn, elf=a.elf, module=module, layout=layout, fixed=fixed, eph=eph, secret=secret,
                secret_pos=a.secret_pos, trial_timeout=a.trial_timeout, active=active, out_buf=out_buf,
                word_size=ws, diff_mode=dm, n_words=active_len // ws, sweep=list(sweep),
                bg_field_mod=QI.infer_field_mod(sdist), seeds=a.seeds, n_eph=n_eph,
                it_blocks=emu.it_blocks(m.function_insns(fn)),
                grid=[(p, j) for p in a.seeds for j in range(n_eph)], traces=a.traces,
                info={"layout_source": source, "layout": layout, "active_lengths": active,
                      "calibration_failed": cal_failed, "out_buf": out_buf, "active_len": active_len,
                      "word_size": ws, "diff_mode": dm, "n_secret_values": len(sweep),
                      "secret_distribution": sdist, "seeds": a.seeds, "eph_draws": n_eph}), t_cal, m


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--elf", required=True, help="ARM ELF with DWARF line info")
    ap.add_argument("--ir", required=True, help=".ll the ELF was compiled from (function signatures, call sites)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "dilithium.json"))
    ap.add_argument("--inputs", default=os.path.join(ROOT, "data", "function_inputs"))
    ap.add_argument("--witness-dir", help="QEMU-test tests_dilithium/ (qemu_witness.json, active_lengths.json), if available")
    ap.add_argument("--functions", help="comma-separated (full names or without the pqcrystals_dilithium2_ref_ prefix)")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2, 3])
    ap.add_argument("--eph-draws", type=int, default=4)
    ap.add_argument("--field-mod", type=int, default=256, help="number of secret values swept at most")
    ap.add_argument("--secret-pos", type=int, default=0)
    ap.add_argument("--traces", type=int, default=16, help="direct_leak trace pairs")
    ap.add_argument("--trial-timeout", type=float, default=300.0, help="wall seconds per trial (QEMU-test: 300)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    a = ap.parse_args(argv)
    a.elf, a.ir = os.path.abspath(a.elf), os.path.abspath(a.ir)
    os.makedirs(a.out, exist_ok=True)

    module = ir.parse_module(open(a.ir).read())
    cfg = json.load(open(a.config))
    prefix = "pqcrystals_dilithium2_ref_"
    names = [n.strip() for n in a.functions.split(",")] if a.functions else list(cfg)
    names = [n if n in cfg else prefix + n for n in names]

    # Rows are reused only if everything that can change them is the same.
    h = hashlib.sha256()
    for p in [a.elf, a.ir, a.config] + sorted(
            os.path.join(d, f) for d in filter(None, (a.inputs, a.witness_dir)) for d, _, fs in os.walk(d) for f in fs) + [
            os.path.join(HERE, f) for f in ("emu.py", "qemu_inputs.py", "detectors.py", "ir.py", "find_faults.py")]:
        h.update(hashlib.sha256(open(p, "rb").read()).digest())
    key = json.dumps({"content": h.hexdigest(), "unicorn": unicorn.__version__, "capstone": capstone.__version__,
                      "seeds": a.seeds, "eph": a.eph_draws, "field_mod": a.field_mod, "pos": a.secret_pos,
                      "traces": a.traces, "trial_timeout": a.trial_timeout,
                      "caller": os.environ.get("MBC_PREFER_CALLER")}, sort_keys=True)
    t_gold = {}  # fn -> [wall, cpu] seconds of its unfaulted runs
    ck_path = os.path.join(a.out, "checkpoint.jsonl")
    done = {}
    if os.path.exists(ck_path):
        for line in open(ck_path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("key") != key:
                continue
            if "row" in r:
                done[(r["row"]["function"], r["row"]["pc"])] = r["row"]
            else:
                t_gold.update(r["golden"])
    ck = open(ck_path, "a")
    ck.write("\n")

    # All functions share one queue of golden runs and one queue of instructions,
    # so a slow instruction never leaves the other workers idle.
    timing, infos, insns, t_cal = [], {}, {}, {}
    t_start = time.perf_counter()
    for fn in names:
        if fn not in cfg:
            timing.append({"function": fn, "status": "error: not in config"})
            continue
        try:
            ctx, t_cal[fn], m = setup_function(a, module, cfg, fn)
            insns[fn] = m.function_insns(fn)
        except Exception as exc:  # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}".replace(ROOT + os.sep, "")
            print(f"[!] {fn}: {msg}", flush=True)
            timing.append({"function": fn, "status": f"error: {msg}"})
            continue
        infos[fn] = ctx.pop("info")
        ctx["golden"] = {}
        _ALL[fn] = ctx
        n_done = sum((fn, pc) in done for pc, _, _ in insns[fn])
        print(f"{fn}: {len(insns[fn])} instructions ({n_done} done), {len(ctx['sweep'])} secret values x "
              f"{len(ctx['grid'])} (p, r), out={ctx['out_buf']} ws={ctx['word_size']} {ctx['diff_mode']}, "
              f"calibration {t_cal[fn][0]:.1f}s", flush=True)
    todo = [(fn, pc, asm) for fn in insns for pc, _, asm in insns[fn] if (fn, pc) not in done]
    need = sorted({t[0] for t in todo})
    gtasks = [(fn, i, p, j) for fn in need for i in range(len(_ALL[fn]["sweep"])) for p, j in _ALL[fn]["grid"]]
    for fn in need:
        t_gold[fn] = [0.0, 0.0]
    if gtasks:
        with mp.get_context("fork").Pool(a.jobs) as pool:
            for (fn, i, p, j), res, dt, dc in pool.imap_unordered(_golden, gtasks, chunksize=8):
                _ALL[fn]["golden"][(i, p, j)] = res
                t_gold[fn][0] += dt
                t_gold[fn][1] += dc
        ck.write(json.dumps({"key": key, "golden": t_gold}) + "\n")
        print(f"unfaulted runs: {len(gtasks)} in {time.perf_counter() - t_start:.0f}s", flush=True)
    # costliest functions first (golden time per instruction sweep), so stragglers start early
    todo.sort(key=lambda t: -t_gold[t[0]][0])
    if todo:
        with mp.get_context("fork").Pool(a.jobs) as pool:
            for n, row in enumerate(pool.imap_unordered(_pc, todo), 1):
                done[(row["function"], row["pc"])] = row
                ck.write(json.dumps({"key": key, "row": row}) + "\n")
                ck.flush()
                print(f"  [{n}/{len(todo)}] {row['function'][26:]} {row['pc']:#07x} {row['asm']:<28} "
                      f"ine={row['ineffective']} cor={row['correction']} dl={row['direct_leak']} "
                      f"trials={row['trials']} {row['seconds']:.1f}s", flush=True)
    rows = []
    for fn in insns:
        fn_rows = [done[(fn, pc)] for pc, _, _ in insns[fn]]
        rows.extend(fn_rows)
        timing.append({
            "function": fn, "status": "ok",
            "seconds": round(t_cal[fn][0] + t_gold[fn][0] + sum(r["seconds"] for r in fn_rows), 3),
            "cpu_seconds": round(t_cal[fn][1] + t_gold[fn][1] + sum(r["cpu_seconds"] for r in fn_rows), 3),
            "calibration_seconds": round(t_cal[fn][0], 3), "golden_seconds": round(t_gold[fn][0], 3),
            "n_pcs": len(fn_rows), "trials": sum(r["trials"] for r in fn_rows),
            "emu_insns": sum(r["emu_insns"] for r in fn_rows), "jobs": a.jobs})
    ck.close()
    print(f"total elapsed {time.perf_counter() - t_start:.0f}s", flush=True)

    with open(os.path.join(a.out, "machine_faults.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({**r, "pc": f"{r['pc']:#x}"})
    tf = ["function", "status", "seconds", "cpu_seconds", "calibration_seconds",
          "golden_seconds", "n_pcs", "trials", "emu_insns", "jobs"]
    with open(os.path.join(a.out, "timing_dilithium.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=tf)
        w.writeheader()
        w.writerows(timing)
    old = {}
    lp = os.path.join(a.out, "layouts.json")
    if os.path.exists(lp):
        old = json.load(open(lp))
    old.update(infos)
    with open(lp, "w") as f:
        json.dump(old, f, indent=1, default=str)
    print(f"{len(rows)} instructions -> {a.out}/machine_faults.csv")


if __name__ == "__main__":
    main()
