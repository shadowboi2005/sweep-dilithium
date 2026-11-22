"""The IR-level run's three tests, ported from QEMU-test (pinned beaf3cc).

  ineffective  dist_tests/common/ineffective.py (IneffectiveTracker) as used by
               early_stop_sweep_unicorn.py: for EVERY public seed p there are
               s1, s2 with the fault ineffective on all ephemeral draws r under
               s1 and effective on all r under s2, at one output word.
  correction   early_stop_sweep_unicorn.py: exists s and alpha != 0 with
               Delta(s, p, r) = alpha (xor or mod-q subtraction) on every
               (p, r) of the grid, at one output word.
  direct_leak  dist_tests/direct_leak.py: every output position the fault
               changed holds the secret (then each eph_secret) at the same
               index, in every one of up to 16 trace pairs taken round-robin
               from the sweep's (p, r) directories, and the secret values there
               are not all the same.
Detail strings use the same wording as the report.
"""
import random

import numpy as np

Q = 8380417


def decode_words(raw, word_size):
    b = bytes(raw)
    if word_size == 1:
        return np.frombuffer(b, dtype=np.uint8).astype(np.int64)
    n = len(b) // word_size
    return np.frombuffer(b[:n * word_size], dtype="<i2" if word_size == 2 else "<i4").astype(np.int64)


def get_buffer(record, name):
    if name in record["outputs"]:
        return record["outputs"][name]
    return record["inputs"][name]


def compute_delta(co, fo, diff_mode, modulus=Q):
    return np.bitwise_xor(co, fo) if diff_mode == "xor" else np.mod(co - fo, modulus)


class IneffectiveTracker:
    def __init__(self, n_pos, n_seeds):
        shape = (n_seeds, n_pos)
        self.has_inef = np.zeros(shape, bool)
        self.has_eff = np.zeros(shape, bool)
        self.val_inef = np.zeros(shape, np.int64)
        self.val_eff = np.zeros(shape, np.int64)

    def update(self, sv, eq):
        """eq: bool (n_seeds, n_eph, n) for secret value sv."""
        eq = np.asarray(eq, bool)
        n = eq.shape[2]
        inef, eff = eq.all(axis=1), (~eq).all(axis=1)
        sl = (slice(None), slice(0, n))
        new = inef & ~self.has_inef[sl]
        self.val_inef[sl][new] = sv
        self.has_inef[sl] |= inef
        new = eff & ~self.has_eff[sl]
        self.val_eff[sl][new] = sv
        self.has_eff[sl] |= eff

    def hit(self):
        pos = np.flatnonzero((self.has_inef & self.has_eff).all(axis=0))
        if pos.size == 0:
            return None
        p = int(pos[0])
        return p, [(int(self.val_inef[k, p]), int(self.val_eff[k, p])) for k in range(self.val_inef.shape[0])]


class EarlyStopSweep:
    """Feeds one secret value at a time; stops when every requested test has
    a witness (--require all)."""

    def __init__(self, seeds, n_eph, active_len_words, diff_mode, modulus=Q):
        self.seeds, self.n_eph = list(seeds), n_eph
        self.grid = [(p, j) for p in self.seeds for j in range(n_eph)]
        self.n_words = active_len_words
        self.diff_mode, self.modulus = diff_mode, modulus
        self.tracker = IneffectiveTracker(active_len_words, len(self.seeds))
        self.ineffective = None
        self.correction = None

    def feed(self, sv, trials):
        """trials[(p, j)] = (correct_words, faulty_words).  True once both tests hit."""
        deltas = [compute_delta(trials[k][0], trials[k][1], self.diff_mode, self.modulus) for k in self.grid]
        co, fo = trials[(self.seeds[0], 0)]
        n_pos = min([self.n_words, len(co == fo)] + [len(d) for d in deltas])
        if self.ineffective is None:
            self.tracker.update(sv, np.stack([np.stack([(trials[(p, j)][0] == trials[(p, j)][1])[:n_pos]
                                                        for j in range(self.n_eph)]) for p in self.seeds]))
            self.ineffective = self.tracker.hit()
        if self.correction is None:
            for pos in range(n_pos):
                alpha = int(deltas[0][pos])
                if alpha != 0 and all(int(d[pos]) == alpha for d in deltas):
                    self.correction = (pos, sv, alpha)
                    break
        return self.ineffective is not None and self.correction is not None

    def verdicts(self):
        K, J = len(self.seeds), self.n_eph
        if self.ineffective:
            pos, wit = self.ineffective
            ine = ("yes", f"DETECTED at pos {pos} (s1={wit[0][0]} ineffective, s2={wit[0][1]} effective; "
                          f"pair on each of {K} public seeds, constant over {J} ephemeral draw(s))")
        else:
            ine = ("no", "no disagreement found in range swept")
        if self.correction:
            pos, sv, alpha = self.correction
            # QEMU-test prints "xor" here even in mod-sub mode; kept for identical details.
            cor = ("yes", f"SATISFIED at pos {pos} (s={sv}, alpha={alpha}: y1 = y2 xor alpha for all {K} swept p x {J} "
                          f"ephemeral draw(s))")
        else:
            cor = ("no", "no (s, alpha) with constant Delta across p found in range swept")
        return ine, cor


# --- direct leak (dist_tests/direct_leak.py) -------------------------------------

def trace_pairs(dirs, limit=16):
    """existing_trace_pairs(): dirs = {dir_path: [(sv_index, correct, faulty)]}.
    Each directory's pairs (in sv order) are shuffled with one Random(0) in
    directory order, then taken round-robin across directories."""
    rng = random.Random(0)
    per_dir = []
    for d in sorted(dirs):
        pairs = [(c, f) for _, c, f in sorted(dirs[d], key=lambda t: t[0])]
        if pairs:
            rng.shuffle(pairs)
            per_dir.append(pairs)
    out = []
    for i in range(max((len(p) for p in per_dir), default=0)):
        for pairs in per_dir:
            if i < len(pairs):
                out.append(pairs[i])
                if 0 < limit <= len(out):
                    return out
    return out


def _reference(buf, correct):
    ins, outs = correct["inputs"], correct["outputs"]
    for name in (buf, f"{buf}_pre"):
        if name in ins:
            return bytes(ins[name]), f"input '{name}'"
    if buf in outs:
        return bytes(outs[buf]), f"correct output '{buf}'"
    return None, f"'{buf}' not in the trace"


def _fmt(pos):
    if len(pos) <= 6:
        return "[" + ", ".join(map(str, pos)) + "]"
    return f"[{pos[0]}, {pos[1]}, ..., {pos[-1]}] ({len(pos)} positions)"


def _check_buffer(buf, skip_outputs, traces):
    refs = []
    for c, f in traces:
        value, where = _reference(buf, c)
        if value is not None:
            refs.append((value, where, c, f))
    if not refs:
        return "skip", f"'{buf}' not in any trace"
    where = refs[0][1]
    outputs = sorted({n for _, _, _, f in refs for n in f["outputs"]} - skip_outputs)
    hits, constant, changed_any = [], [], False
    for name in outputs:
        applicable, values, positions = 0, set(), None
        for value, _, c, f in refs:
            co, fo = bytes(c["outputs"].get(name, b"")), bytes(f["outputs"].get(name, b""))
            n = min(len(co), len(fo))
            changed = [j for j in range(n) if co[j] != fo[j]]
            if not changed:
                continue
            changed_any = True
            if not all(j < len(value) and fo[j] == value[j] for j in changed):
                applicable = 0
                break
            applicable += 1
            values.add(tuple(value[j] for j in changed if j < len(value)))
            positions = positions or changed
        if not applicable:
            continue
        msg = (f"output '{name}' at {_fmt(positions)} holds {where} at the same index in all "
               f"{applicable} trace(s) where the fault changed it")
        (hits if len(values) > 1 else constant).append(msg)
    if hits:
        return "yes", "; ".join(hits)
    if constant:
        return "no", constant[0] + ", but the value never varied across the traces, so a constant could match by chance"
    if not changed_any:
        return "no", "the fault changed no output in the traces"
    return "no", f"the changed output positions do not hold {where}"


def direct_leak(secret_buf, eph_bufs, traces):
    checked = [("secret-buf", secret_buf)] + [("eph_secret", e) for e in eph_bufs]
    skip = {b for _, b in checked}
    results = []
    for role, buf in checked:
        v, d = _check_buffer(buf, skip, traces)
        if v == "yes":
            return "yes", f"{role}: {d}"
        results.append((v, f"{role}: {d}"))
    if any(v == "no" for v, _ in results):
        return "no", "; ".join(d for v, d in results if v == "no")
    return "skip", "; ".join(d for _, d in results)
