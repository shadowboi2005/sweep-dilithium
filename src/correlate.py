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
import os
import random
import re
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ir  # noqa: E402
from tables import read_csv, write_csv  # noqa: E402

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
    for r in read_csv(path):
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


CORRELATION_FIELDS = [
    "kind", "swept_function", "pc", "asm", "ir_function", "ir_index", "ir_inst", "match", "fault",
    "test", "ir_detected", "mc_detected", "own_function", "agree", "fired", "mc_ineffective", "mc_correction",
    "mc_direct_leak", "ir_detail", "mc_detail"]


class _IRText:
    """The text of IR instructions, empty where there is none."""

    def __init__(self, module):
        self._functions = module.functions

    def __call__(self, function, index):
        f = self._functions.get(function)
        return f.insts[index].text.strip() if f and index < len(f.insts) else ""


def _matched_row(base, fault, row, inst):
    test, own = row["test"], fault["function"] == base["ir_function"]
    comparable = own and _decided(fault, test)
    return {**base, "kind": "matched", "ir_index": row["ir_index"], "ir_inst": inst, "fault": row["fault"], "test": test,
            "ir_detected": row["detected"], "mc_detected": fault[test], "own_function": "yes" if own else "no",
            "agree": _agree(row["detected"], fault[test]) if comparable else "-",
            "ir_detail": row["detail"], "mc_detail": fault[f"{test}_detail"]}


def _machine_row(base, indices, inst_text):
    texts = [inst_text(base["ir_function"], i) for i in indices]
    return {**base, "kind": "machine_only", "ir_index": ";".join(map(str, indices)), "ir_inst": " | ".join(texts)}


def _ir_only_row(row, swept, inst_text):
    why = "no PC maps to this IR instruction" if row["function"] in swept else "function not swept"
    return {"kind": "ir_only", "ir_function": row["function"], "ir_index": row["ir_index"],
            "ir_inst": inst_text(row["function"], row["ir_index"]), "fault": row["fault"], "test": row["test"],
            "ir_detected": row["detected"], "ir_detail": row["detail"], "match": why}


def _machine_verdict(row, own, other, swept):
    """What the PCs of a report row say: yes / no, or why there is no verdict."""
    if any(f[row["test"]] == "yes" for f in own):
        return "yes"
    if any(_decided(f, row["test"]) for f in own):
        return "no"
    if own:
        return "not executed" if all(f["fired"] == "0" for f in own) else "undecided"
    if row["function"] not in swept:
        return "not swept"
    return "inlined only" if other else "unmapped"


def _report_row(row, faults, swept):
    # Only the function's own instructions were tested on the row's inputs.
    # Copies inlined into other functions ran under those functions' tests.
    own = [f for f in faults if f["function"] == row["function"]]
    other = [f for f in faults if f["function"] != row["function"]]
    test, verdict = row["test"], _machine_verdict(row, own, other, swept)
    return {"function": row["function"], "fault": row["fault"], "family": row["family"], "model": row["model"],
            "test": test, "ir_index": row["ir_index"], "ir_detected": row["detected"], "n_pcs": len(own),
            "n_pcs_decided": sum(_decided(f, test) for f in own),
            "n_pcs_detected": sum(f[test] == "yes" for f in own), "pcs": " ".join(f["pc"] for f in own),
            "mc_any": verdict, "agree": _agree(row["detected"], verdict), "n_inlined_pcs": len(other),
            "n_inlined_detected": sum(f[test] == "yes" for f in other)}


def cmd_map(a):
    with open(a.ir) as f:
        module = ir.parse_module(f.read())
    pc_to_ir, inst_text = ir.PcToIR(module, a.elf), _IRText(module)
    faults, report = read_csv(a.faults), load_report(a.report)
    swept = {f["function"] for f in faults}
    rows_at, faults_at = defaultdict(list), defaultdict(list)  # (function, IR index) -> ...
    for r in report:
        rows_at[r["function"], r["ir_index"]].append(r)

    out = []
    for f in faults:
        ir_fn, indices, match = pc_to_ir.lookup(int(f["pc"], 16), f["function"])
        base = {"swept_function": f["function"], "pc": f["pc"], "asm": f["asm"], "ir_function": ir_fn,
                "match": match, "fired": f["fired"], **{f"mc_{t}": f[t] for t in TESTS}}
        for i in indices:
            faults_at[ir_fn, i].append(f)
        matched = [_matched_row(base, f, r, inst_text(ir_fn, i)) for i in indices for r in rows_at.get((ir_fn, i), [])]
        out += matched or [_machine_row(base, indices, inst_text)]
    out += [_ir_only_row(r, swept, inst_text) for r in report if (r["function"], r["ir_index"]) not in faults_at]
    rows = [_report_row(r, faults_at.get((r["function"], r["ir_index"]), []), swept) for r in report]
    write_csv(os.path.join(a.out, "correlation.csv"), out, CORRELATION_FIELDS)
    write_csv(os.path.join(a.out, "report_rows.csv"), rows)

    print(f"correlation.csv: {dict(Counter(o['kind'] for o in out))}")
    print("report rows (IR verdict -> any mapped PC reproduces it):")
    for t in TESTS:
        c = Counter((x["ir_detected"], x["mc_any"]) for x in rows if x["test"] == t)
        print(f"  {t:12} " + ", ".join(f"{i}->{m}: {n}" for (i, m), n in sorted(c.items())))


def cmd_compare(a):
    tables = []
    for path in a.rows:
        label = os.path.basename(os.path.dirname(os.path.abspath(path))) or path
        tables.append((label, {(r["fault"], r["test"]): r for r in read_csv(path)}))
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
    write_csv(os.path.join(a.out, "opt_compare.csv"), rows)
    for label, t in tables:
        c = Counter((r["ir_detected"], r["mc_any"]) for r in t.values())
        print(f"{label}: " + ", ".join(f"{i}->{m}: {n}" for (i, m), n in sorted(c.items())))


def cmd_timing(a):
    irt = {r["function"]: r for r in read_csv(a.ir_timing)}
    n_faults = Counter()
    if a.report:
        seen = set()
        for r in read_csv(a.report):
            if (r["function"], r["fault"]) not in seen:
                seen.add((r["function"], r["fault"]))
                n_faults[r["function"]] += 1
    ours = []
    for path in a.ours:
        label = os.path.basename(os.path.dirname(os.path.abspath(path)))
        ours.append((label, {r["function"]: r for r in read_csv(path)}))
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
            for k in ("seconds", "cpu_seconds", "n_pcs", "trials", "emu_insns"):
                r[f"{label}_{k}"] = o.get(k, "")
            if o.get("seconds") and o.get("n_pcs"):
                r[f"{label}_seconds_per_pc"] = round(float(o["seconds"]) / int(o["n_pcs"]), 3)
            if o.get("seconds") and r["ir_seconds"]:
                r[f"{label}_ir_over_ours"] = round(float(r["ir_seconds"]) / max(float(o["seconds"]), 1e-9), 2)
        rows.append(r)
    write_csv(os.path.join(a.out, "timing_compare.csv"), rows)
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


def _row_sets(rows):
    return {"all rows": rows, "call skips (fnSkip)": [r for r in rows if r["model"] == "fnSkip"],
            "value faults": [r for r in rows if r["model"] != "fnSkip"],
            "without *uniform*": [r for r in rows if "uniform" not in r["function"]],
            **{t: [r for r in rows if r["test"] == t] for t in TESTS}}


def _pairs(rows):
    return [(r["ir_detected"], r["mc_any"]) for r in rows]


def _site_pairs(rows):
    """One unit per (IR site, test): "yes" if any fault model there is "yes"."""
    site = defaultdict(list)
    for r in rows:
        site[r["function"], r["ir_index"], r["test"]].append(r)
    return [tuple("yes" if any(r[k] == "yes" for r in rs) else "no" for k in ("ir_detected", "mc_any"))
            for rs in site.values()]


def _random_pc_null(rows, faults, draws, one_pc=False):
    """Agreement with random PCs of the same function instead of the mapped
    ones, out of those that count for the observed rows (executed, not only
    crashes): as many as are mapped, or just one.  The more PCs a row maps to,
    the more often "any PC says yes" holds by chance; one PC has no such bias."""
    pool, groups = defaultdict(list), defaultdict(list)
    for f in faults:
        if _decided(f, "ineffective"):
            pool[f["function"]].append(f)
    for r in rows:
        groups[r["function"], r["pcs"], int(r["n_pcs_decided"])].append(r)
    rng, scores = random.Random(0), []
    for _ in range(draws):
        pairs = []
        for (fn, _, n), rs in groups.items():  # one draw for all tests of a site, as observed
            pick = rng.sample(pool[fn], min(1 if one_pc else n, len(pool[fn])))
            pairs += [(r["ir_detected"], "yes" if any(f[r["test"]] == "yes" for f in pick) else "no") for r in rs]
        scores.append(_score(pairs))
    observed = _score(_pairs(rows))

    def summary(key, digits):
        values = [s[key] or 0.0 for s in scores]
        return round(sum(values) / draws, digits), round(sum(v >= (observed[key] or 0.0) for v in values) / draws, 4)

    (agree, p_agree), (kappa, p_kappa) = summary("agree", 1), summary("kappa", 3)
    return {"n": observed["n"], "agree": agree, "kappa": kappa, "p_agree": p_agree, "p_kappa": p_kappa}


def _outcomes_of_yes_rows(rows):
    yes = [r for r in rows if r["ir_detected"] == "yes" and r["mc_any"] != "not swept"]
    outcomes = Counter(r["mc_any"] for r in yes)
    return {"n": len(yes), "tp": outcomes["yes"], **{"mc_" + k.replace(" ", "_"): v for k, v in outcomes.items()}}


def cmd_stats(a):
    """Report-row agreement of each build, next to trivial baselines."""
    out = []
    for d in a.dirs:
        label = os.path.basename(os.path.abspath(d))
        rows = read_csv(os.path.join(d, "report_rows.csv"))
        compared = [r for r in rows if r["agree"] in ("yes", "no")]
        if not compared:
            print(f"{label}: no comparable rows")
            continue
        faults = read_csv(os.path.join(d, "machine_faults.csv"))
        results = {name: _score(_pairs(rs)) for name, rs in _row_sets(compared).items()}
        results["(IR site, test) units"] = _score(_site_pairs(compared))
        for name in ("all rows", "call skips (fnSkip)", "value faults"):
            rs = _row_sets(compared)[name]
            if rs:
                results[f"{name}: as many random PCs"] = _random_pc_null(rs, faults, a.draws)
                results[f"{name}: one random PC"] = _random_pc_null(rs, faults, a.draws, one_pc=True)
        results["IR yes rows of swept functions"] = _outcomes_of_yes_rows(rows)
        out += [{"build": label, "set": name, **r} for name, r in results.items()]
    write_csv(os.path.join(a.out, "stats.csv"), out)
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
