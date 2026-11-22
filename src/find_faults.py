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
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import capstone  # noqa: E402
import unicorn  # noqa: E402

import ir  # noqa: E402
import qemu_inputs  # noqa: E402
import scheme  # noqa: E402
import sweep  # noqa: E402
from checkpoint import Checkpoint  # noqa: E402
from emu import Machine  # noqa: E402
from tables import write_csv  # noqa: E402

TIMING_FIELDS = ["function", "status", "seconds", "cpu_seconds", "calibration_seconds",
                 "golden_seconds", "n_pcs", "trials", "emu_insns", "jobs"]


def parse_args(argv):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--elf", required=True, help="ARM ELF with DWARF line info")
    ap.add_argument("--ir", required=True, help=".ll the ELF was compiled from (function signatures, call sites)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(ROOT, "config", "dilithium.json"))
    ap.add_argument("--inputs", default=os.path.join(ROOT, "data", "function_inputs"))
    ap.add_argument("--witness-dir", help="QEMU-test tests_dilithium/ (qemu_witness.json, active_lengths.json), if available")
    ap.add_argument("--functions", help=f"comma-separated (full names or without the {scheme.PREFIX} prefix)")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2, 3])
    ap.add_argument("--eph-draws", type=int, default=4)
    ap.add_argument("--field-mod", type=int, default=256, help="number of secret values swept at most")
    ap.add_argument("--secret-pos", type=int, default=0)
    ap.add_argument("--traces", type=int, default=16, help="direct_leak trace pairs")
    ap.add_argument("--trial-timeout", type=float, default=300.0, help="wall seconds per trial (QEMU-test: 300)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    a = ap.parse_args(argv)
    a.elf, a.ir = os.path.abspath(a.elf), os.path.abspath(a.ir)
    return a


def checkpoint_key(a):
    """Rows are reused only if everything that can change them is the same."""
    files = [a.elf, a.ir, a.config]
    for top in filter(None, (a.inputs, a.witness_dir)):
        files += sorted(os.path.join(d, f) for d, _, names in os.walk(top) for f in names)
    files += sorted(os.path.join(HERE, f) for f in os.listdir(HERE) if f.endswith(".py"))
    content = hashlib.sha256()
    for path in files:
        with open(path, "rb") as f:
            content.update(hashlib.sha256(f.read()).digest())
    return json.dumps({"content": content.hexdigest(), "unicorn": unicorn.__version__, "capstone": capstone.__version__,
                       "seeds": a.seeds, "eph": a.eph_draws, "field_mod": a.field_mod, "pos": a.secret_pos,
                       "traces": a.traces, "trial_timeout": a.trial_timeout,
                       "caller": os.environ.get("MBC_PREFER_CALLER")}, sort_keys=True)


# --- setup ---------------------------------------------------------------------------

def _names(text):
    return [s for s in (text or "").split(",") if s]


def build_layout(a, module, function):
    """(layout, where it comes from)"""
    layout = qemu_inputs.load_witness(a.witness_dir, function)
    if layout is not None:
        return layout, "witness"
    sample = qemu_inputs.load_sample(a.inputs, function)
    return qemu_inputs.IRLayout(module).build(function, sample), "rebuilt from IR + function_inputs"


def check_secret_is_used(secret, layout, active_lengths, secret_pos):
    n = active_lengths.get(secret)  # None: not calibrated, the whole buffer is filled
    if n is not None and n <= secret_pos and layout[secret].get("type") != "scalar":
        raise ValueError(f"secret-buf {secret!r} has active length {n}: no secret value would ever reach the function")


def setup_function(a, module, entry, function):
    """(context, description for layouts.json, calibration seconds), as QEMU-test derives them."""
    secret, eph = entry["secret-buf"], _names(entry.get("eph_secret"))
    fixed = _names(entry.get("fixed-scalars"))
    layout, source = build_layout(a, module, function)
    if secret not in layout:
        raise ValueError(f"secret-buf {secret!r} not in layout {list(layout)}")
    for e in eph:
        if e not in layout:
            raise ValueError(f"eph_secret {e!r} not in layout")
    machine = Machine(a.elf)
    instructions = machine.function_insns(function)
    harness = qemu_inputs.Harness(machine, module, function, layout, fixed, eph, a.trial_timeout)
    watch = sweep.Stopwatch()
    active, failed = qemu_inputs.load_active_lengths(a.witness_dir, function), {}
    if active is None:
        active, failed = qemu_inputs.calibrate(harness, a.field_mod)
    calibration = watch.read()
    check_secret_is_used(secret, layout, active, a.secret_pos)
    out_buf, active_len, word_size, diff_mode, _ = qemu_inputs.derive_all(layout, secret, a.field_mod)
    distribution = layout[secret].get("distribution")
    ctx = sweep.FunctionContext(
        function=function, elf=a.elf, module=module, layout=layout, fixed_scalars=fixed, eph_secrets=eph,
        secret=secret, secret_pos=a.secret_pos, secret_values=list(qemu_inputs.domain_values(distribution, a.field_mod)),
        active_lengths=active, background_mod=qemu_inputs.infer_field_mod(distribution), seeds=a.seeds,
        n_eph=a.eph_draws if eph else 1, out_buf=out_buf, word_size=word_size, n_words=active_len // word_size,
        diff_mode=diff_mode, n_traces=a.traces, trial_timeout=a.trial_timeout, instructions=instructions)
    info = {"layout_source": source, "layout": layout, "active_lengths": active, "calibration_failed": failed,
            "out_buf": out_buf, "active_len": active_len, "word_size": word_size, "diff_mode": diff_mode,
            "n_secret_values": len(ctx.secret_values), "secret_distribution": distribution,
            "seeds": ctx.seeds, "eph_draws": ctx.n_eph}
    return ctx, info, calibration


def setup_all(a, checkpoint):
    """Fill sweep.CONTEXTS.  Returns (layout infos, calibration seconds, error rows)."""
    with open(a.ir) as f:
        module = ir.parse_module(f.read())
    with open(a.config) as f:
        config = json.load(f)
    names = [n.strip() for n in a.functions.split(",")] if a.functions else list(config)
    infos, calibration, errors = {}, {}, []
    sweep.CONTEXTS.clear()
    for function in (scheme.full_name(n, config) for n in names):
        try:
            if function not in config:
                raise ValueError("not in config")
            ctx, infos[function], calibration[function] = setup_function(a, module, config[function], function)
        except Exception as exc:  # noqa: BLE001  (one bad function must not stop the others)
            message = f"{type(exc).__name__}: {exc}".replace(ROOT + os.sep, "")
            print(f"[!] {function}: {message}", flush=True)
            errors.append({"function": function, "status": f"error: {message}"})
            continue
        sweep.CONTEXTS[function] = ctx
        n_done = sum((function, pc) in checkpoint.rows for pc, _, _ in ctx.instructions)
        print(f"{function}: {len(ctx.instructions)} instructions ({n_done} done), {len(ctx.secret_values)} secret values x "
              f"{len(ctx.grid)} (p, r), out={ctx.out_buf} ws={ctx.word_size} {ctx.diff_mode}, "
              f"calibration {calibration[function][0]:.1f}s", flush=True)
    return infos, calibration, errors


# --- the two pools ---------------------------------------------------------------------
# All functions share one queue of golden runs and one queue of instructions, so a
# slow instruction never leaves the other workers idle.

def run_golden_pool(functions, jobs, checkpoint):
    tasks = [(fn, i, p, j) for fn in functions for i in range(len(sweep.CONTEXTS[fn].secret_values))
             for p, j in sweep.CONTEXTS[fn].grid]
    if not tasks:
        return
    started, seconds = time.perf_counter(), {fn: [0.0, 0.0] for fn in functions}
    with mp.get_context("fork").Pool(jobs) as pool:
        for (fn, i, p, j), result, (wall, cpu) in pool.imap_unordered(sweep.run_golden, tasks, chunksize=8):
            sweep.CONTEXTS[fn].golden[i, p, j] = result
            seconds[fn][0] += wall
            seconds[fn][1] += cpu
    checkpoint.add_golden_seconds(seconds)
    print(f"unfaulted runs: {len(tasks)} in {time.perf_counter() - started:.0f}s", flush=True)


def run_instruction_pool(tasks, jobs, checkpoint):
    if not tasks:
        return
    # costliest functions first, so stragglers start early
    tasks = sorted(tasks, key=lambda t: -checkpoint.golden_seconds[t[0]][0])
    with mp.get_context("fork").Pool(jobs) as pool:
        for n, row in enumerate(pool.imap_unordered(sweep.sweep_instruction, tasks), 1):
            checkpoint.add_row(row)
            print(f"  [{n}/{len(tasks)}] {scheme.short_name(row['function'])} {row['pc']:#07x} {row['asm']:<28} "
                  f"ine={row['ineffective']} cor={row['correction']} dl={row['direct_leak']} "
                  f"trials={row['trials']} {row['seconds']:.1f}s", flush=True)


# --- outputs -----------------------------------------------------------------------------

def timing_row(function, rows, calibration, golden, jobs):
    """Sequential-equivalent cost: calibration + unfaulted runs + every instruction."""
    return {"function": function, "status": "ok",
            "seconds": round(calibration[0] + golden[0] + sum(r["seconds"] for r in rows), 3),
            "cpu_seconds": round(calibration[1] + golden[1] + sum(r["cpu_seconds"] for r in rows), 3),
            "calibration_seconds": round(calibration[0], 3), "golden_seconds": round(golden[0], 3),
            "n_pcs": len(rows), "trials": sum(r["trials"] for r in rows),
            "emu_insns": sum(r["emu_insns"] for r in rows), "jobs": jobs}


def update_json(path, entries):
    old = {}
    if os.path.exists(path):
        with open(path) as f:
            old = json.load(f)
    with open(path, "w") as f:
        json.dump({**old, **entries}, f, indent=1, default=str)


def main(argv=None):
    a = parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    started = time.perf_counter()
    checkpoint = Checkpoint(os.path.join(a.out, "checkpoint.jsonl"), checkpoint_key(a))
    try:
        infos, calibration, timing = setup_all(a, checkpoint)
        todo = [(fn, pc, asm) for fn, ctx in sweep.CONTEXTS.items() for pc, _, asm in ctx.instructions
                if (fn, pc) not in checkpoint.rows]
        run_golden_pool(sorted({fn for fn, _, _ in todo}), a.jobs, checkpoint)
        run_instruction_pool(todo, a.jobs, checkpoint)
    finally:
        checkpoint.close()
    print(f"total elapsed {time.perf_counter() - started:.0f}s", flush=True)

    rows = []
    for fn, ctx in sweep.CONTEXTS.items():
        fn_rows = [checkpoint.rows[fn, pc] for pc, _, _ in ctx.instructions]
        timing.append(timing_row(fn, fn_rows, calibration[fn], checkpoint.golden_seconds[fn], a.jobs))
        rows += fn_rows
    write_csv(os.path.join(a.out, "machine_faults.csv"), [{**r, "pc": f"{r['pc']:#x}"} for r in rows], sweep.ROW_FIELDS)
    write_csv(os.path.join(a.out, "timing_dilithium.csv"), timing, TIMING_FIELDS)
    update_json(os.path.join(a.out, "layouts.json"), infos)
    print(f"{len(rows)} instructions -> {a.out}/machine_faults.csv")


if __name__ == "__main__":
    main()
