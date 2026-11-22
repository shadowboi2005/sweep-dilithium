#!/usr/bin/env python3
"""Step 2: correlate the machine-level faults with the IR-level report.

  src/correlate.py map --faults out/O0/machine_faults.csv --elf ELF --ir RELABELLED.ll \
                   --report fault_report_dilithium.csv --out out/O0
      each PC -> (IR function, IR instruction index) through DWARF, joined with
      the report rows of that instruction:
        correlation.csv  kind = matched | machine_only | ir_only, one row per
                         (PC, report row) / unmatched PC / unmatched report row
        report_rows.csv  one row per report row: how many PCs of its own function
                         map to its IR instruction, how many reproduce its test,
                         agree?  (copies inlined into other functions are counted
                         apart: they ran under those functions' tests)
  src/correlate.py compare out/O0/report_rows.csv out/O3/report_rows.csv --out out
        opt_compare.csv  the report rows side by side for the two builds
  src/correlate.py timing --ir-timing data/timing_dilithium.csv \
                      --ours out/O0/timing_dilithium.csv [...] --report ... --out out
        timing_compare.csv  IR-level vs emulation seconds per function
  src/correlate.py stats out/O0 out/O3 --out out
        stats.csv  agreement with the report next to always-"no" and random-PC baselines
"""
import argparse
import csv
import os
import random
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ir  # noqa: E402

TESTS = ("ineffective", "correction", "direct_leak")
FAMILIES = ("binOpFault", "loadStoreSkip", "loopOrFuncSkip")


def parse_fault(fault, function):
    """(family, ir_index, model, callee) of `<family>/<fn>_line<N>_<model>` or
    `<family>/<fn>_fnSkip_<callee>_line<N>`."""
    family, _, rest = fault.partition("/")
    if family not in FAMILIES or not rest.startswith(function + "_"):
        raise ValueError(f"unrecognised fault name {fault!r}")
    rest = rest[len(function) + 1:]
    m = re.fullmatch(r"fnSkip_(.+)_line(\d+)", rest)
    if m:
        return family, int(m.group(2)), "fnSkip", m.group(1)
    m = re.fullmatch(r"line(\d+)_(\w+)", rest)
    if m:
        return family, int(m.group(1)), m.group(2), ""
    raise ValueError(f"unrecognised fault name {fault!r}")


def load_report(path):
    rows = []
    for r in csv.DictReader(open(path, newline="")):
        family, idx, model, callee = parse_fault(r["fault"], r["function"])
        rows.append({**r, "detected": r["detected"].strip().lower(), "family": family, "ir_index": idx,
                     "model": model, "callee": callee})
    return rows


def _agree(a, b):
    return "yes" if a == b else ("no" if a in ("yes", "no") and b in ("yes", "no") else "-")


def _decided(f, test):
    """The skip ran and the test reached a verdict (not only crashes or timeouts)."""
    if f["fired"] == "0" or f[test] not in ("yes", "no"):
        return False
    return test == "direct_leak" or int(f["sv_examined"]) > 0


def cmd_map(a):
    mod = ir.parse_module(open(a.ir).read())
    p2i = ir.PcToIR(mod, a.elf)
    faults = list(csv.DictReader(open(a.faults, newline="")))
    report = load_report(a.report)
    by_site = defaultdict(list)
    for r in report:
        by_site[(r["function"], r["ir_index"])].append(r)
    swept = {f["function"] for f in faults}

    out, covered = [], set()
    pcs_at = defaultdict(list)  # (fn, idx) -> machine rows
    for f in faults:
        pc = int(f["pc"], 16)
        ir_fn, idxs, match = p2i.lookup(pc, f["function"])
        base = {"swept_function": f["function"], "pc": f["pc"], "asm": f["asm"], "ir_function": ir_fn,
                "match": match, "fired": f["fired"],
                **{f"mc_{t}": f[t] for t in TESTS}}
        hit = False
        for idx in idxs:
            pcs_at[(ir_fn, idx)].append(f)
            inst = mod.functions[ir_fn].insts[idx].text.strip() if ir_fn in mod.functions else ""
            for r in by_site.get((ir_fn, idx), []):
                hit = True
                covered.add(id(r))
                out.append({**base, "kind": "matched", "ir_index": idx, "ir_inst": inst, "fault": r["fault"],
                            "test": r["test"], "ir_detected": r["detected"], "mc_detected": f[r["test"]],
                            "agree": _agree(r["detected"], f[r["test"]]), "ir_detail": r["detail"],
                            "mc_detail": f[f"{r['test']}_detail"]})
        if not hit:
            out.append({**base, "kind": "machine_only", "ir_index": ";".join(map(str, idxs)),
                        "ir_inst": " | ".join(mod.functions[ir_fn].insts[i].text.strip() for i in idxs)
                        if ir_fn in mod.functions else ""})
    for r in report:
        if id(r) not in covered:
            why = "function not swept" if r["function"] not in swept else "no PC maps to this IR instruction"
            out.append({"kind": "ir_only", "ir_function": r["function"], "ir_index": r["ir_index"],
                        "ir_inst": (mod.functions[r["function"]].insts[r["ir_index"]].text.strip()
                                    if r["function"] in mod.functions and r["ir_index"] < len(mod.functions[r["function"]].insts) else ""),
                        "fault": r["fault"], "test": r["test"], "ir_detected": r["detected"],
                        "ir_detail": r["detail"], "match": why})

    cols = ["kind", "swept_function", "pc", "asm", "ir_function", "ir_index", "ir_inst", "match", "fault",
            "test", "ir_detected", "mc_detected", "agree", "fired", "mc_ineffective", "mc_correction",
            "mc_direct_leak", "ir_detail", "mc_detail"]
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "correlation.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out)

    rr = []
    for r in report:
        at = pcs_at.get((r["function"], r["ir_index"]), [])
        # Only the function's own instructions were tested on the row's inputs.
        # Copies inlined into other functions ran under those functions' tests.
        ms = [f for f in at if f["function"] == r["function"]]
        other = [f for f in at if f["function"] != r["function"]]
        n_det = sum(f[r["test"]] == "yes" for f in ms)
        if n_det:
            mc_any = "yes"
        elif any(_decided(f, r["test"]) for f in ms):
            mc_any = "no"
        elif ms:
            mc_any = "not executed" if all(f["fired"] == "0" for f in ms) else "undecided"
        elif r["function"] not in swept:
            mc_any = "not swept"
        else:
            mc_any = "inlined only" if other else "unmapped"
        rr.append({"function": r["function"], "fault": r["fault"], "family": r["family"], "model": r["model"],
                   "test": r["test"], "ir_index": r["ir_index"], "ir_detected": r["detected"], "n_pcs": len(ms),
                   "n_pcs_detected": n_det, "pcs": " ".join(f["pc"] for f in ms), "mc_any": mc_any,
                   "agree": _agree(r["detected"], mc_any), "n_inlined_pcs": len(other),
                   "n_inlined_detected": sum(f[r["test"]] == "yes" for f in other)})
    with open(os.path.join(a.out, "report_rows.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rr[0]))
        w.writeheader()
        w.writerows(rr)

    kinds = Counter(o["kind"] for o in out)
    print(f"correlation.csv: {dict(kinds)}")
    print("report rows (IR verdict -> any mapped PC reproduces it):")
    for t in TESTS:
        c = Counter((x["ir_detected"], x["mc_any"]) for x in rr if x["test"] == t)
        print(f"  {t:12} " + ", ".join(f"{i}->{m}: {n}" for (i, m), n in sorted(c.items())))


def cmd_compare(a):
    tables = []
    for path in a.rows:
        label = os.path.basename(os.path.dirname(os.path.abspath(path))) or path
        tables.append((label, {(r["fault"], r["test"]): r for r in csv.DictReader(open(path, newline=""))}))
    keys = list(tables[0][1])
    for _, t in tables[1:]:
        keys += [k for k in t if k not in keys]
    rows = []
    for k in keys:
        first = next(t[k] for _, t in tables if k in t)
        row = {"fault": k[0], "test": k[1], "function": first["function"], "ir_detected": first["ir_detected"]}
        for label, t in tables:
            r = t.get(k, {})
            row.update({f"{label}_n_pcs": r.get("n_pcs", ""), f"{label}_n_detected": r.get("n_pcs_detected", ""),
                        f"{label}_mc_any": r.get("mc_any", "")})
        rows.append(row)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "opt_compare.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    for label, t in tables:
        c = Counter((r["ir_detected"], r["mc_any"]) for r in t.values())
        print(f"{label}: " + ", ".join(f"{i}->{m}: {n}" for (i, m), n in sorted(c.items())))


def cmd_timing(a):
    irt = {r["function"]: r for r in csv.DictReader(open(a.ir_timing, newline=""))}
    n_faults = Counter()
    if a.report:
        seen = set()
        for r in csv.DictReader(open(a.report, newline="")):
            if (r["function"], r["fault"]) not in seen:
                seen.add((r["function"], r["fault"]))
                n_faults[r["function"]] += 1
    ours = []
    for path in a.ours:
        label = os.path.basename(os.path.dirname(os.path.abspath(path)))
        ours.append((label, {r["function"]: r for r in csv.DictReader(open(path, newline=""))}))
    fns = list(irt) + [f for _, t in ours for f in t if f not in irt]
    rows = []
    for fn in fns:
        r = {"function": fn, "ir_status": irt.get(fn, {}).get("status", ""), "ir_seconds": irt.get(fn, {}).get("seconds", ""),
             "ir_faults": n_faults.get(fn, "")}
        if r["ir_seconds"] and r["ir_faults"]:
            r["ir_seconds_per_fault"] = round(float(r["ir_seconds"]) / r["ir_faults"], 3)
        for label, t in ours:
            o = t.get(fn, {})
            r[f"{label}_status"] = o.get("status", "")
            for k in ("seconds", "cpu_seconds", "elapsed_seconds", "n_pcs", "trials", "emu_insns"):
                r[f"{label}_{k}"] = o.get(k, "")
            if o.get("seconds") and o.get("n_pcs"):
                r[f"{label}_seconds_per_pc"] = round(float(o["seconds"]) / int(o["n_pcs"]), 3)
            if o.get("seconds") and r["ir_seconds"]:
                r[f"{label}_ir_over_ours"] = round(float(r["ir_seconds"]) / max(float(o["seconds"]), 1e-9), 2)
        rows.append(r)
    cols = []
    for r in rows:
        cols += [k for k in r if k not in cols]
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "timing_compare.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    both = [r for r in rows if r["ir_seconds"] and all(r.get(f"{lab}_seconds") for lab, _ in ours)]
    if both:
        s_ir = sum(float(r["ir_seconds"]) for r in both)
        msg = ", ".join(f"{lab} {sum(float(r[f'{lab}_seconds']) for r in both):.0f}s" for lab, _ in ours)
        print(f"{len(both)} functions in both: IR-level {s_ir:.0f}s; ours (sequential-equivalent) {msg}")


def _score(pairs):
    """2x2 counts, agreement, the always-"no" baseline and Cohen's kappa."""
    c = Counter(pairs)
    tp, fn, fp, tn = c["yes", "yes"], c["yes", "no"], c["no", "yes"], c["no", "no"]
    n = tp + fn + fp + tn
    out = {"n": n, "tp": tp, "fn": fn, "fp": fp, "tn": tn}
    if n:
        po = (tp + tn) / n
        pe = ((tp + fn) * (tp + fp) + (fp + tn) * (fn + tn)) / n ** 2
        out.update(agree=tp + tn, always_no=fp + tn, agree_rate=round(po, 3), always_no_rate=round((fp + tn) / n, 3),
                   precision=round(tp / (tp + fp), 3) if tp + fp else "", recall=round(tp / (tp + fn), 3) if tp + fn else "",
                   kappa=round((po - pe) / (1 - pe), 3) if pe < 1 else "")
    return out


def cmd_stats(a):
    """Report-row agreement of each build, next to trivial baselines."""
    out = []
    for d in a.dirs:
        label = os.path.basename(os.path.abspath(d))
        rows = list(csv.DictReader(open(os.path.join(d, "report_rows.csv"), newline="")))
        faults = list(csv.DictReader(open(os.path.join(d, "machine_faults.csv"), newline="")))
        dec = [r for r in rows if r["agree"] in ("yes", "no")]
        sets = {"all rows": dec, "call skips (fnSkip)": [r for r in dec if r["model"] == "fnSkip"],
                "value faults": [r for r in dec if r["model"] != "fnSkip"],
                "without *uniform*": [r for r in dec if "uniform" not in r["function"]]}
        sets.update({t: [r for r in dec if r["test"] == t] for t in TESTS})
        for name, rs in sets.items():
            out.append({"build": label, "set": name, **_score([(r["ir_detected"], r["mc_any"]) for r in rs])})
        # one unit per (IR site, test): IR "yes" if any fault model there is "yes"
        site = defaultdict(set)
        for r in dec:
            site[r["function"], r["ir_index"], r["test"]].add((r["ir_detected"], r["mc_any"]))
        out.append({"build": label, "set": "IR sites",
                    **_score([(max(i for i, _ in v), max(m for _, m in v)) for v in site.values()])})
        # null: as many random executed PCs of the same function instead of the mapped ones
        by_fn = defaultdict(list)
        for f in faults:
            by_fn[f["function"]].append(f)
        rng, agree, kappa = random.Random(0), [], []
        groups = defaultdict(list)
        for r in dec:
            groups[r["function"], r["pcs"], int(r["n_pcs"])].append(r)
        for _ in range(a.draws):
            pairs = []
            for (fn, _, n), rs in groups.items():
                pick = rng.sample(by_fn[fn], min(n, len(by_fn[fn])))
                pairs += [(r["ir_detected"], "yes" if any(f[r["test"]] == "yes" for f in pick) else "no") for r in rs]
            s = _score(pairs)
            agree.append(s["agree"])
            kappa.append(s["kappa"])
        obs = _score([(r["ir_detected"], r["mc_any"]) for r in dec])
        out.append({"build": label, "set": f"random PCs of the same function (mean of {a.draws})", "n": obs["n"],
                    "agree": round(sum(agree) / len(agree), 1), "kappa": round(sum(kappa) / len(kappa), 3),
                    "p_agree": round(sum(x >= obs["agree"] for x in agree) / len(agree), 4),
                    "p_kappa": round(sum(x >= obs["kappa"] for x in kappa) / len(kappa), 4)})
        yes = [r for r in rows if r["ir_detected"] == "yes"]
        out.append({"build": label, "set": "all IR yes rows", "n": len(yes), "tp": sum(r["mc_any"] == "yes" for r in yes),
                    **{"mc_" + k.replace(" ", "_"): v for k, v in Counter(r["mc_any"] for r in yes).items()}})
    cols = []
    for r in out:
        cols += [k for k in r if k not in cols]
    with open(os.path.join(a.out, "stats.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out)
    print(f"{len(out)} rows -> {a.out}/stats.csv")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("map")
    m.add_argument("--faults", required=True)
    m.add_argument("--elf", required=True)
    m.add_argument("--ir", required=True, help="the (relabelled) .ll the ELF was built from")
    m.add_argument("--report", required=True)
    m.add_argument("--out", required=True)
    c = sub.add_parser("compare")
    c.add_argument("rows", nargs="+", help="report_rows.csv of each build (label = its directory name)")
    c.add_argument("--out", required=True)
    t = sub.add_parser("timing")
    t.add_argument("--ir-timing", required=True)
    t.add_argument("--ours", nargs="+", required=True, help="timing_dilithium.csv of each build")
    t.add_argument("--report", help="fault report, to count IR faults per function")
    t.add_argument("--out", required=True)
    s = sub.add_parser("stats")
    s.add_argument("dirs", nargs="+", help="output directories of `map` (label = directory name)")
    s.add_argument("--out", required=True)
    s.add_argument("--draws", type=int, default=2000)
    a = ap.parse_args(argv)
    {"map": cmd_map, "compare": cmd_compare, "timing": cmd_timing, "stats": cmd_stats}[a.cmd](a)


if __name__ == "__main__":
    main()
