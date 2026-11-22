"""Inputs exactly as the IR-level run (QEMU-test, pinned beaf3cc) builds them.

Ported, behaviour-preserving, from QEMU-test:
  * distribution samplers, domain_values, infer_field_mod
        dist_tests/dilithium/setup/distributions_unicorn.py
  * whole-coefficient secret override
        dist_tests/dilithium/setup/collect_dist_unicorn.py (_dilithium_override_hook)
  * per-trial sampling order (scalars, then pointer buffers, in layout order;
    ephemeral buffers redrawn from Random("eph:j"); fill length from the
    calibrated active length) and the probe mode used by calibration
        dist_tests/common/unicorn_runner.py (run_trial)
  * active-length calibration
        dist_tests/dilithium/setup/calibrate_unicorn.py (calibrate_buffer)
  * out_buf / word size / diff mode / active length / field mod
        dist_tests/dilithium/run_combined_unicorn.sh (derive_all)
  * the argument layout (qemu_witness.json): no witness files are available,
    so it is rebuilt the way the harness generator and extract_qemu_witness.py
    would build it -- argument order and roles from the function_inputs
    sample, pointer lengths from the alloca the first call site's argument
    traces back to in the IR (Dilithium-LLVM fault-types:
    createDynamicDriverFunction / traceArgToRoot / pickCallSite /
    inferPointerAllocSize).  A real witness file is used instead when given.

The only difference from QEMU-test is how the function is entered: QEMU-test
runs a generated main() that calls it; here it is called directly with the
same argument values and buffer contents.
"""
import json
import os
import random
import re

from emu import EmulationError, EmulationTimeout
from scheme import Q

# --- Dilithium2 parameters / distributions (distributions_unicorn.py) -----------

D = 13
K_, L_, ETA, TAU, BETA, GAMMA1, GAMMA2, OMEGA = 4, 4, 2, 39, 78, 1 << 17, (Q - 1) // 88, 80
COEFF_BYTES = 4


def random_fill(rng, field_mod, length):
    return [rng.randrange(field_mod) for _ in range(length)]


def _int32_le(val):
    val &= 0xFFFFFFFF
    return [val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >> 24) & 0xFF]


def _pack(coeffs):
    out = []
    for c in coeffs:
        out.extend(_int32_le(c))
    return out


def _challenge(rng, n):
    coeffs = [0] * n
    if n:
        for idx in rng.sample(range(n), min(TAU, n)):
            coeffs[idx] = 1 if rng.randrange(2) else -1
    return _pack(coeffs)


def _hint(rng, n):
    coeffs = [0] * n
    if n:
        for idx in rng.sample(range(n), rng.randrange(min(OMEGA, n) + 1)):
            coeffs[idx] = 1
    return _pack(coeffs)


_TABLE = {
    "uniform in [-eta, eta]": ("uniform_eta", None),
    "uniform in [-eta,eta]": ("uniform_eta", None),
    "secret key polynomial, uniform in [-eta, eta]": ("uniform_eta", None),
    "centered binomial distribution, eta": ("uniform_eta", None),
    "r_q = z_q[x]/(x^n + 1)": ("poly_mod_q", None),
    "r_q = z_q[x]/(x^n + 1), coefficient domain": ("poly_mod_q", None),
    "r_q = z_q[x]/(x^n + 1), ntt domain": ("poly_mod_q", None),
    "r_q = z_q[x]/(x^n + 1), montgomery domain": ("poly_signed", Q - 1),
    "r_q = z_q[x]/(x^n + 1), reduced coefficients": ("poly_signed", Q // 2),
    "r_q = z_q[x]/(x^n + 1), reduced": ("poly_signed", Q // 2),
    "uniform polynomial matrix in r_q": ("poly_mod_q", None),
    "uniform polynomial in r_q": ("poly_mod_q", None),
    "uniform in (-gamma1, gamma1]": ("poly_gamma1", None),
    "masking vector y, uniform in (-gamma1, gamma1]": ("poly_gamma1", None),
    "z = y + c*s1, coefficients bounded by gamma1 - beta": ("poly_signed", GAMMA1 - BETA),
    "coefficients bounded by gamma1 - beta": ("poly_signed", GAMMA1 - BETA),
    "coefficients bounded by gamma2 - beta": ("poly_signed", GAMMA2 - BETA),
    "c*s1, coefficients bounded by beta": ("poly_signed", BETA),
    "coefficients bounded by beta": ("poly_signed", BETA),
    "high bits w1, coefficients in [0, (q-1)/(2*gamma2))": ("w1", None),
    "w1 high bits": ("w1", None),
    "low bits w0, coefficients bounded by gamma2": ("poly_signed", GAMMA2),
    "challenge polynomial with tau +-1 coefficients": ("challenge", None),
    "sparse challenge polynomial": ("challenge", None),
    "hint polynomial, 0/1 coefficients": ("hint", None),
    "hint vector with at most omega nonzero coefficients": ("hint", None),
    "t0, coefficients in (-2^(d-1), 2^(d-1)]": ("t0", None),
    "t1, coefficients in [0, 2^10)": ("t1", None),
    "uniform 256-bit byte string": ("bytes", None),
    "uniform byte string": ("bytes", None),
    "seed byte string": ("bytes", None),
    "message byte string": ("bytes", None),
    "randomness byte string": ("bytes", None),
    "crh byte string": ("bytes", None),
    "tr byte string": ("bytes", None),
    "dilithium public key byte string": ("bytes", None),
    "dilithium secret key byte string": ("bytes", None),
    "dilithium signature byte string": ("bytes", None),
    "packed polynomial byte string": ("bytes", None),
    "keccak state": ("bytes", None),
}
_QUALIFIER = re.compile(r"\s+(poly|polynomial|polyvec|polyveck|polyvecl|vector|matrix)$")


def lookup(dist):
    if not dist:
        return None
    key = re.sub(r"\s+", " ", dist.strip().lower())
    e = _TABLE.get(key)
    if e is None:
        s = _QUALIFIER.sub("", key)
        if s != key:
            e = _TABLE.get(s)
    return e


def sample_for_distribution(dist, length, rng, field_mod):
    e = lookup(dist)
    if e is None:
        return random_fill(rng, field_mod, length)
    kind, extra = e
    n = length // COEFF_BYTES
    if kind == "bytes":
        out = [rng.randrange(256) for _ in range(length)]
    elif kind == "uniform_eta":
        out = _pack([rng.randrange(-ETA, ETA + 1) for _ in range(n)])
    elif kind == "poly_mod_q":
        out = _pack([rng.randrange(0, Q) for _ in range(n)])
    elif kind == "poly_signed":
        out = _pack([rng.randrange(-extra, extra + 1) for _ in range(n)])
    elif kind == "poly_gamma1":
        out = _pack([rng.randrange(-GAMMA1 + 1, GAMMA1 + 1) for _ in range(n)])
    elif kind == "w1":
        out = _pack([rng.randrange(0, (Q - 1) // (2 * GAMMA2)) for _ in range(n)])
    elif kind == "challenge":
        out = _challenge(rng, n)
    elif kind == "hint":
        out = _hint(rng, n)
    elif kind == "t0":
        half = 1 << (D - 1)
        out = _pack([rng.randrange(-half + 1, half + 1) for _ in range(n)])
    elif kind == "t1":
        out = _pack([rng.randrange(0, 1 << 10) for _ in range(n)])
    else:
        raise ValueError(kind)
    if len(out) < length:
        out.extend(rng.randrange(256) for _ in range(length - len(out)))
    return out[:length]


def domain_bounds(dist):
    e = lookup(dist)
    if e is None or e[0] == "bytes":
        return 0, 255
    kind, extra = e
    return {
        "uniform_eta": (-ETA, ETA), "poly_mod_q": (0, Q - 1), "poly_signed": (-(extra or 0), extra or 0),
        "poly_gamma1": (-GAMMA1 + 1, GAMMA1), "w1": (0, (Q - 1) // (2 * GAMMA2) - 1),
        "challenge": (-1, 1), "hint": (0, 1), "t0": (-(1 << (D - 1)) + 1, 1 << (D - 1)), "t1": (0, (1 << 10) - 1),
    }.get(kind, (0, 255))


def infer_field_mod(dist):
    lo, hi = domain_bounds(dist)
    return hi - lo + 1


def domain_values(dist, count):
    """At most `count` secret values in the distribution's range, 0 first."""
    lo, hi = domain_bounds(dist)
    size = hi - lo + 1
    if count >= size:
        zero = lo <= 0 <= hi
        return ([0] if zero else []) + [v for v in range(lo, hi + 1) if not (zero and v == 0)]
    out, seen = [], set()

    def add(v):
        if v not in seen and len(out) < count:
            seen.add(v)
            out.append(v)

    if lo <= 0 <= hi:
        add(0)
    n = max(count, 2)
    for i in range(n):
        add(lo + (i * (size - 1)) // (n - 1))
    v = lo
    while len(out) < count:
        add(v)
        v += 1
    return out


def is_coeff_shaped(dist):
    e = lookup(dist)
    return e is not None and e[0] != "bytes"


def apply_override(spec, vals, pos, val):
    """Whole int32 coefficient for a coefficient-shaped buffer, else one byte."""
    if not 0 <= pos < len(vals):
        return vals
    if is_coeff_shaped(spec.get("distribution")):
        base = (pos // COEFF_BYTES) * COEFF_BYTES
        vals[base:base + COEFF_BYTES] = _int32_le(val)
    else:
        vals[pos] = val & 0xFF
    return vals


# --- argument layout (harness generator + extract_qemu_witness.py) -----------------

WITNESS_TYPE_SIZES = {"i8": 1, "i16": 2, "i32": 4, "i64": 8, "ptr": 8}  # extract_qemu_witness.py
DL_SIZES = {"i1": 1, "i8": 1, "i16": 2, "i32": 4, "i64": 8, "ptr": 4, "float": 4, "double": 8}  # thumbv7em datalayout
RESERVED = {"output", "distribution"}


def _split_top(s, sep=","):
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == sep and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += ch in "([{<"
        depth -= ch in ")]}>"
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _array_dims(t):
    """'[4 x [256 x i32]]' -> (1024, 'i32'); 'i32' -> (None, 'i32')."""
    t = t.strip()
    if not t.startswith("["):
        return None, t
    m = re.match(r"\[\s*(\d+)\s*x\s*(.*)\]$", t, re.S)
    n, inner = int(m.group(1)), m.group(2).strip()
    c, base = _array_dims(inner)
    return n * (c or 1), base


def _dl_size(t):
    n, base = _array_dims(t)
    m = re.match(r"<\s*(\d+)\s*x\s*(\w+)\s*>", base)
    if m:
        return (n or 1) * int(m.group(1)) * DL_SIZES.get(m.group(2), 4)
    return (n or 1) * DL_SIZES.get(base, 4)


def _gep_offset(src_ty, idxs):
    """Constant byte offset of a GEP, or None if an index is not constant."""
    off, ty = 0, src_ty.strip()
    for k, idx in enumerate(idxs):
        m = re.match(r"i\d+\s+(-?\d+)$", idx.strip())
        if not m:
            return None
        v = int(m.group(1))
        if k == 0:
            off += v * _dl_size(ty)
        else:
            mm = re.match(r"\[\s*\d+\s*x\s*(.*)\]$", ty, re.S)
            if not mm:
                return None
            ty = mm.group(1).strip()
            off += v * _dl_size(ty)
    return off


_GEP = re.compile(r"getelementptr(?:\s+(?:inbounds|nuw|nusw|inrange\(\S+\)))*\s+(.*)$")


def _parse_gep(rhs):
    """'getelementptr inbounds i8, ptr %a, i32 4' -> (src_type, base, [idx...])."""
    m = _GEP.match(rhs.strip())
    if not m:
        return None
    parts = _split_top(m.group(1))
    if len(parts) < 2:
        return None
    base = parts[1].split()[-1]
    return parts[0], base, parts[2:]


class IRLayout:
    """Rebuilds QEMU-test's qemu_witness.json layout for a function."""

    def __init__(self, module):
        self.mod = module
        self._defs = {}
        self._calls = None
        self._memo = {}
        self._globals = {}
        for line in module.lines:
            m = re.match(r"^(@[\w.$-]+) = .*?\b(?:global|constant)\s+(\[[^=]*?\]|i\d+|ptr)(?:\s|,|$)", line)
            if m:
                self._globals[m.group(1)] = m.group(2)

    def _fn_defs(self, fn):
        d = self._defs.get(fn.name)
        if d is None:
            d = {}
            for inst in fn.insts:
                m = re.match(r"\s*(%[\w.]+) = (.*)$", inst.text)
                if m:
                    d[m.group(1)] = m.group(2)
            self._defs[fn.name] = d
        return d

    def _call_sites(self):
        if self._calls is None:
            self._calls = {}
            for fn in self.mod.functions.values():
                for inst in fn.insts:
                    for m in re.finditer(r"\bcall\b[^@]*@([\w.$-]+)\(", inst.text):
                        self._calls.setdefault(m.group(1), []).append((inst.lineno, fn, inst))
        return self._calls

    def pick_call_site(self, fname):
        """CallBase F->users().front(): LLVM's use-list order after parsing
        textual IR -- uses after F's definition newest first, then forward
        references in textual order.  MBC_PREFER_CALLER overrides (as in
        the generator)."""
        if fname in self._memo:
            return self._memo[fname]
        sites = sorted(self._call_sites().get(fname, []), key=lambda s: s[0])
        fn = self.mod.functions.get(fname)
        chosen = None
        if sites:
            dl = fn.lineno if fn else -1
            after = [s for s in sites if s[0] > dl][::-1]
            before = [s for s in sites if s[0] < dl]
            order = after + before
            chosen = order[0]
            pref = os.environ.get("MBC_PREFER_CALLER")
            if pref:
                chosen = next((s for s in order if pref in s[1].name), chosen)
        self._memo[fname] = chosen
        return chosen

    @staticmethod
    def call_operands(call_text, fname):
        i = call_text.index(f"@{fname}(") + len(fname) + 2
        depth, j = 1, i
        while depth:
            depth += call_text[j] == "("
            depth -= call_text[j] == ")"
            j += 1
        return _split_top(call_text[i:j - 1])

    def trace_root(self, operand, caller):
        """traceArgToRoot: through GEPs/bitcasts and up through arguments.
        Returns ('alloca', type) | ('global', type) | ('other', None)."""
        seen = set()
        tok = operand.split()[-1] if operand else ""
        if "getelementptr" in operand:
            g = re.search(r"ptr\s+([%@][\w.$-]+)", operand[operand.index("getelementptr"):])
            tok = g.group(1) if g else tok
        fn = caller
        while (fn.name, tok) not in seen:
            seen.add((fn.name, tok))
            if tok.startswith("@"):
                ty = self._globals.get(tok)
                return ("global", ty) if ty else ("other", None)
            rhs = self._fn_defs(fn).get(tok)
            if rhs is not None:
                if rhs.startswith("alloca"):
                    m = re.match(r"alloca\s+(?:inalloca\s+)?(\[.*?\]|i\d+|ptr|\S+?)(?:,|$)", rhs)
                    return "alloca", m.group(1).strip() if m else None
                gep = _parse_gep(rhs)
                if gep:
                    tok = gep[1]
                    continue
                m = re.match(r"bitcast\s+\S+\s+([%@][\w.$-]+)\s+to", rhs)
                if m:
                    tok = m.group(1)
                    continue
                return "other", None
            names = [p[1] for p in fn.params]
            if tok.startswith("%") and tok[1:] in names:
                site = self.pick_call_site(fn.name)
                if site is None:
                    return "other", None
                ops = self.call_operands(site[2].text, fn.name)
                k = names.index(tok[1:])
                if k >= len(ops):
                    return "other", None
                op = ops[k]
                tok = op.split()[-1]
                if "getelementptr" in op:
                    g = re.search(r"ptr\s+([%@][\w.$-]+)", op[op.index("getelementptr"):])
                    tok = g.group(1) if g else tok
                fn = site[1]
                continue
            return "other", None
        return "other", None

    def infer_pointer_alloc_size(self, fn, arg, default=128):
        defs = self._fn_defs(fn)
        users = {}
        for name, rhs in defs.items():
            g = _parse_gep(rhs)
            if g:
                users.setdefault(g[1], []).append((name, g))
        loads, stores = {}, {}
        for inst in fn.insts:
            t = inst.text.strip()
            m = re.match(r"(?:%[\w.]+ = )?load(?: volatile)?\s+([^,]+),\s*ptr\s+(%[\w.]+)", t)
            if m:
                loads.setdefault(m.group(2), []).append(m.group(1))
            m = re.match(r"store(?: volatile)?\s+(\S+(?:\s*x\s*\S+)?)\s+[^,]+,\s*ptr\s+(%[\w.]+)", t)
            if m:
                stores.setdefault(m.group(2), []).append(m.group(1))
        best, found, seen = 0, False, set()

        def scan(v):
            nonlocal best, found
            if v in seen:
                return
            seen.add(v)
            for name, (src, _, idxs) in users.get(v, []):
                off = _gep_offset(src, idxs)
                if off is not None:
                    acc = off + 1
                    for ty in loads.get(name, []):
                        acc = off + _dl_size(ty)
                    for ty in stores.get(name, []):
                        acc = off + _dl_size(ty)
                    if acc > best:
                        best, found = acc, True
                scan(name)
            for name, rhs in defs.items():
                m = re.match(rf"bitcast\s+\S+\s+{re.escape(v)}\s+to", rhs)
                if m:
                    scan(name)

        scan(f"%{arg}")
        return best if found else default

    def build(self, fname, sample):
        """The layout dict, in the witness's order."""
        fn = self.mod.functions[fname]
        outs = sample["output"]
        outs = outs if isinstance(outs, list) else [outs]
        scalar_return = outs == ["return"]
        keys = list(sample.keys())
        snapshot = {k for k in keys if k.endswith("_in") and k[:-3] in keys}
        pkeys = [k for k in keys if k not in RESERVED and k not in snapshot]
        if len(pkeys) != len(fn.params):
            raise ValueError(f"{fname}: sample has {len(pkeys)} params {pkeys}, IR has {len(fn.params)}")
        dists = sample.get("distribution") or {}
        site = self.pick_call_site(fname)
        ops = self.call_operands(site[2].text, fname) if site else []
        layout = {}
        for i, (key, (ty, pname)) in enumerate(zip(pkeys, fn.params)):
            if ty == "ptr":
                kind, rty = self.trace_root(ops[i], site[1]) if i < len(ops) else ("other", None)
                spec = {"role": "input"}
                n, base = _array_dims(rty) if rty else (None, None)
                if kind in ("alloca", "global") and n is not None:
                    spec["length"] = n * WITNESS_TYPE_SIZES.get(base, 1)
                elif kind in ("alloca", "global") and rty and re.match(r"^(i\d+|ptr)$", rty):
                    spec["length"] = WITNESS_TYPE_SIZES.get(rty, 4)
                    v = sample.get(key)
                    if isinstance(v, int) and not isinstance(v, bool):
                        spec["type"], spec["init_value"] = "scalar_ptr", v
                else:
                    spec["length"] = self.infer_pointer_alloc_size(fn, pname)
                spec["_root"] = f"{kind}:{rty}" + (f" via {site[1].name}" if site else "")
            else:
                width = max(1, int(re.match(r"i(\d+)", ty).group(1)) // 8) if re.match(r"i\d+", ty) else 4
                v = sample.get(key)
                spec = {"role": "input", "type": "scalar", "length": width,
                        "init_value": v if isinstance(v, int) and not isinstance(v, bool) else 0,
                        "_signext": "signext" in self._param_text(fn, i)}
            if key in dists:
                spec["distribution"] = dists[key]
            if f"{key}_in" in dists:
                post = spec.get("distribution")
                spec["distribution"] = dists[f"{key}_in"]
                if post is not None:
                    spec["output_distribution"] = post
            layout[key] = spec
        if scalar_return:
            layout["return"] = {"role": "output", "length": 1, "type": "scalar"}
        else:
            for o in outs:
                layout[o]["role"] = "output"
                if f"{o}_in" in snapshot:
                    layout[o]["also_input"] = True
        return layout

    def _param_text(self, fn, i):
        line = self.mod.lines[fn.lineno]
        start = line.index("(", line.index("@"))
        depth = 0
        for j in range(start, len(line)):
            depth += line[j] == "("
            depth -= line[j] == ")"
            if depth == 0:
                break
        parts = _split_top(line[start + 1:j])
        return parts[i] if i < len(parts) else ""


def load_sample(inputs_dir, fname):
    short = re.sub(r"^pqcrystals_dilithium_fips202_ref_|^pqcrystals_dilithium(?:2|3|5)?_ref_", "", fname)
    path = os.path.join(inputs_dir, short + ".json")
    with open(path) as f:
        for line in f:
            if line.strip():
                return json.loads(line)
    raise ValueError(f"{path} has no sample")


def derive_all(layout, secret_buf, field_mod=256):
    """run_combined_unicorn.sh: (out_buf, active_len, word_size, diff_mode, field_mod)."""
    outs = [n for n, s in layout.items() if s.get("role") == "output"]
    out = outs[0]
    spec = layout[out]
    if spec.get("type") == "scalar":
        ln = spec.get("length", 1)
        ws, dm = (ln if ln in (1, 2, 4) else 1), "xor"
    elif (spec.get("output_distribution") or spec.get("distribution")) and \
            infer_field_mod(spec.get("output_distribution") or spec.get("distribution")) != 256:
        ws, dm = 4, "mod-sub"
    else:
        ws, dm = 1, "xor"
    return out, spec["length"], ws, dm, field_mod


# --- one trial (run_trial) --------------------------------------------------------

class Harness:
    """Calls `fname` with QEMU-test's inputs; one Machine reused across trials
    (memory, registers and peripherals restored before each one, so trials
    are independent of their order)."""

    STACK = 0x40000

    def __init__(self, machine, module, fname, layout, fixed, eph, trial_timeout):
        self.m = machine
        self.fn = module.functions[fname]
        self.fname = fname
        self.layout = layout
        self.fixed = set(fixed)
        self.eph = set(eph)
        self.timeout = trial_timeout
        self.ptrs = [n for n, s in layout.items() if s.get("type") != "scalar"]
        self.rng_stream = machine.stub_randombytes()

    def inputs(self, mode, rng_seed, field_mod, active_lengths=None, override=None,
               eph_seed=None, probe=None):
        """{name: bytes} written into each argument, as run_trial writes them.
        override = (buf, pos, val); probe = (buf, length, seed, co_seed)."""
        active_lengths = active_lengths or {}
        if mode == "probe":
            rng, co_rng = random.Random(probe[2]), random.Random(probe[3])
        else:
            rng, co_rng = random.Random(rng_seed), None
        eph_rng = random.Random(eph_seed) if (self.eph and mode == "collect") else None
        written = {}
        for name, spec in self.layout.items():
            if spec.get("type") != "scalar" or spec.get("role") != "input":
                continue
            w = spec.get("length", 1)
            if name in self.fixed:
                v = spec.get("init_value", 0) & ((1 << (8 * w)) - 1)
                vals = [(v >> (8 * i)) & 0xFF for i in range(w)]
            elif mode == "probe":
                r = rng if name == probe[0] else co_rng
                vals = [r.randrange(field_mod) for _ in range(w)]
            else:
                vals = sample_for_distribution(spec.get("distribution"), w, rng, field_mod)
                if override and name == override[0]:
                    vals = apply_override(spec, vals, override[1], override[2])
            written[name] = vals
        for name in self.ptrs:
            spec = self.layout[name]
            also = spec.get("also_input")
            if not (spec.get("role") == "input" or also):
                continue
            if spec.get("type") == "scalar_ptr":
                w = spec.get("length", 4)
                v = spec.get("init_value", 0) & ((1 << (8 * w)) - 1)
                vals = [(v >> (8 * i)) & 0xFF for i in range(w)]
                if mode != "probe" and override and name == override[0]:
                    vals = apply_override(spec, vals, override[1], override[2])
                written[name] = vals
                continue
            if mode == "probe":
                vals = random_fill(rng, field_mod, probe[1]) if name == probe[0] \
                    else random_fill(co_rng, field_mod, spec["length"])
            else:
                n = active_lengths.get(name, spec["length"])
                vals = sample_for_distribution(spec.get("distribution"), n, rng, field_mod)
                if name in self.eph:
                    vals = sample_for_distribution(spec.get("distribution"), n, eph_rng, field_mod)
                if override and name == override[0]:
                    vals = apply_override(spec, vals, override[1], override[2])
            written[f"{name}_pre" if also else name] = vals
        return {k: bytes(v & 0xFF for v in vals) for k, vals in written.items()}

    def run(self, written, restore=False):
        """Execute once.  Returns {"inputs", "outputs"} (bytes values) or raises
        EmulationError (crash) / EmulationTimeout."""
        m = self.m
        if restore:
            m.restore_memory()
        if self.rng_stream is not None:
            self.rng_stream.reset()
        m.scratch_reset()
        args, addrs = [], {}
        for i, (key, spec) in enumerate((k, s) for k, s in self.layout.items() if k != "return"):
            if spec.get("type") == "scalar":
                w = spec["length"]
                raw = written.get(key, bytes(w))
                v = int.from_bytes(raw[:w], "little")
                if spec.get("_signext") and v >> (8 * w - 1):
                    v -= 1 << (8 * w)
                args.append(v)
            else:
                buf = bytearray(spec["length"])
                data = written.get(f"{key}_pre", written.get(key, b""))
                buf[:len(data)] = data[:len(buf)]
                addrs[key] = m.alloc(buf)
                args.append(addrs[key])
        r0 = m.call(self.fname, args, timeout_s=self.timeout, stack_size=self.STACK)
        outs = {}
        for key, spec in self.layout.items():
            if spec.get("role") == "output":
                outs[key] = bytes([r0 & 0xFF]) if key == "return" else m.read(addrs[key], spec["length"])
        return {"inputs": written, "outputs": outs}


# --- calibration (calibrate_unicorn.py) --------------------------------------------

SKIP_CALIBRATION_BELOW = 128
MAX_SAFE_PROBE_LEN = 16384
ZERO_LENGTH_RETRIES = 8


def calibrate(h, field_mod=256):
    """{buffer: active length}: the shortest prefix after which the rest of
    the buffer no longer changes any output (binary search, n_repeats=1)."""
    active, failed = {}, {}
    for name, s in h.layout.items():
        if s.get("role") != "input" and not s.get("also_input"):
            continue
        full = s["length"]
        if s.get("type") == "scalar_ptr" or full <= SKIP_CALIBRATION_BELOW:
            active[name] = full
            continue
        try:
            active[name] = _calibrate_buffer(h, name, full, field_mod)
        except Exception as exc:  # noqa: BLE001  (QEMU-test skips a buffer it cannot calibrate)
            failed[name] = f"{type(exc).__name__}: {exc}"
    return active, failed


def _calibrate_buffer(h, name, full, field_mod):
    eff = min(full, MAX_SAFE_PROBE_LEN)

    def probe(length, seed, co):
        w = h.inputs("probe", 0, field_mod, probe=(name, length, seed, co))
        return h.run(w, restore=True)["outputs"]

    seed = co = 0
    ref = probe(eff, seed, co)

    def matches(length):
        return probe(length, seed, co) == ref

    if matches(0):
        for k in range(1, ZERO_LENGTH_RETRIES + 1):
            seed = co = k
            ref = probe(eff, seed, co)
            if not matches(0):
                break
        else:
            return 0
    lo, hi = 0, 1
    while hi < eff and not matches(hi):
        lo, hi = hi, min(hi * 2, eff)
    if not matches(hi):
        return eff
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if matches(mid):
            hi = mid
        else:
            lo = mid
    return hi


def load_witness(witness_dir, fname):
    if not witness_dir:
        return None
    p = os.path.join(witness_dir, fname, "qemu_witness.json")
    if not os.path.exists(p):
        p = os.path.join(witness_dir, f"{fname}.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)["layout"]
    return None


def load_active_lengths(witness_dir, fname):
    if not witness_dir:
        return None
    p = os.path.join(witness_dir, fname, "active_lengths.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


__all__ = ["Harness", "IRLayout", "calibrate", "derive_all", "domain_values", "infer_field_mod",
           "load_sample", "load_witness", "load_active_lengths", "EmulationError", "EmulationTimeout"]
