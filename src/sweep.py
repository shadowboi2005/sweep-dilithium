"""The work of one task: an unfaulted run, or the sweep of one instruction.

Tasks run in forked workers.  `CONTEXTS` is filled in the parent before the
fork; each worker keeps one Harness (and so one Machine) per function."""
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field

import detectors
import emu
import qemu_inputs
from emu import EmulationError, EmulationTimeout, Machine, Skip

FALLBACK_PAIRS = 4  # direct_leak.py
ROW_FIELDS = ["function", "pc", "asm", "fired", "trials", "sv_examined", "sv_skipped", "crash", "timeout",
              "ineffective", "ineffective_detail", "correction", "correction_detail",
              "direct_leak", "direct_leak_detail", "n_trace_pairs", "seconds", "cpu_seconds", "emu_insns"]


@dataclass
class FunctionContext:
    """Everything the tasks of one function need."""
    function: str
    elf: str
    module: object
    layout: dict
    fixed_scalars: list
    eph_secrets: list
    secret: str
    secret_pos: int
    secret_values: list
    active_lengths: dict
    background_mod: int
    seeds: list
    n_eph: int
    out_buf: str
    word_size: int
    n_words: int
    diff_mode: str
    n_traces: int
    trial_timeout: float
    instructions: list  # [(pc, size, asm)]
    golden: dict = field(default_factory=dict)  # (value index, p, j) -> (status, record or message)

    def __post_init__(self):
        self.grid = [(p, j) for p in self.seeds for j in range(self.n_eph)]
        self.it_blocks = emu.it_blocks(self.instructions)

    def new_harness(self):
        return qemu_inputs.Harness(Machine(self.elf), self.module, self.function, self.layout,
                                   self.fixed_scalars, self.eph_secrets, self.trial_timeout)

    def trial_inputs(self, harness, value_index, p, j):
        return harness.inputs("collect", p, self.background_mod, self.active_lengths,
                              (self.secret, self.secret_pos, self.secret_values[value_index]), f"eph:{j}")

    def fallback_inputs(self, harness, k):
        """direct_leak.py collect_pairs(): seed k, eph draw k, no secret override."""
        return harness.inputs("collect", k, self.background_mod, self.active_lengths, None, f"eph:{k}")

    def output_words(self, record):
        raw = detectors.get_buffer(record, self.out_buf)
        return detectors.decode_words(raw, self.word_size)[:self.n_words]

    def trial_dir(self, p, j):
        """The (p, r) trial directory name QEMU-test would use (for trace order)."""
        parts = ([] if p == self.seeds[0] else [f"seed{p}"]) + ([f"eph{j}"] if j else [])
        return "/".join(parts)


CONTEXTS = {}  # function -> FunctionContext
_harnesses = {}  # (process, function) -> Harness


def _harness(ctx):
    key = os.getpid(), ctx.function
    if key not in _harnesses:
        _harnesses[key] = ctx.new_harness()
    return _harnesses[key]


class Stopwatch:
    """Wall and thread CPU seconds (Unicorn's timeout thread is not counted)."""

    def __init__(self):
        self._wall, self._cpu = time.perf_counter(), time.thread_time()

    def read(self):
        return time.perf_counter() - self._wall, time.thread_time() - self._cpu


def run_golden(task):
    """One unfaulted run: task -> (task, (status, record or message), seconds)."""
    function, value_index, p, j = task
    ctx = CONTEXTS[function]
    harness = _harness(ctx)
    inputs = ctx.trial_inputs(harness, value_index, p, j)
    watch = Stopwatch()
    try:
        result = "ok", harness.run(inputs, restore=True)
    except EmulationTimeout as e:
        result = "timeout", str(e)
    except EmulationError as e:
        result = "fail", str(e)
    return task, result, watch.read()


def sweep_instruction(task):
    """Skip one instruction over the whole sweep: task -> row of machine_faults.csv."""
    function, pc, asm = task
    ctx = CONTEXTS[function]
    harness = _harness(ctx)
    skip = Skip(harness.m, pc, ctx.it_blocks.get(pc))
    try:
        watch, insns_before = Stopwatch(), harness.m.icount
        sweep = InstructionSweep(ctx, harness, skip)
        sweep.run()
        verdicts = sweep.verdicts()
        wall, cpu = watch.read()
        return {"function": function, "pc": pc, "asm": asm, **sweep.counts(), **verdicts,
                "seconds": round(wall, 3), "cpu_seconds": round(cpu, 3),
                "emu_insns": harness.m.icount - insns_before}
    finally:
        skip.detach()


class _ValueFailed(Exception):
    """A trial of this secret value crashed: the value is skipped."""


class InstructionSweep:
    """QEMU-test's early-stop sweep of one faulted instruction, then direct_leak."""

    def __init__(self, ctx, harness, skip):
        self.ctx, self.harness, self.skip = ctx, harness, skip
        self.detectors = detectors.EarlyStopSweep(ctx.seeds, ctx.n_eph, ctx.n_words, ctx.diff_mode)
        self.trace_dirs = defaultdict(list)
        self.fired = self.trials = self.crashes = self.examined = self.skipped = self.n_pairs = 0
        self.timed_out = False

    def run(self):
        """Secret values in order until both tests have a witness; a trial past
        the time limit ends the sweep."""
        try:
            for index, value in enumerate(self.ctx.secret_values):
                try:
                    words = self._run_value(index)
                except _ValueFailed:
                    self.skipped += 1
                    continue
                self.examined += 1
                if self.detectors.feed(value, words):
                    break
        except EmulationTimeout:
            self.timed_out = True

    def _run_value(self, index):
        """{(p, j): (correct words, faulty words)} of one secret value."""
        ctx, words = self.ctx, {}
        for p, j in ctx.grid:
            status, golden = ctx.golden[index, p, j]
            if status == "timeout":
                raise EmulationTimeout(f"correct run: {golden}")
            if status == "fail":
                raise _ValueFailed
            faulty = self._run_faulted(golden["inputs"])
            self.trace_dirs[ctx.trial_dir(p, j)].append((index, golden, faulty))
            words[p, j] = ctx.output_words(golden), ctx.output_words(faulty)
        return words

    def _run_faulted(self, inputs):
        self.skip.reset()
        self.trials += 1
        try:
            return self.harness.run(inputs, restore=True)
        except EmulationError:
            self.crashes += 1
            raise _ValueFailed from None
        finally:
            self.fired += self.skip.fired > 0

    def verdicts(self):
        if self.timed_out:
            ineffective = correction = "timeout", "subprocess.TimeoutExpired"
        else:
            ineffective, correction = self.detectors.verdicts()
        leak = self._direct_leak()
        return {"ineffective": ineffective[0], "ineffective_detail": ineffective[1],
                "correction": correction[0], "correction_detail": correction[1],
                "direct_leak": leak[0], "direct_leak_detail": leak[1], "n_trace_pairs": self.n_pairs}

    def _direct_leak(self):
        pairs = detectors.trace_pairs(self.trace_dirs, self.ctx.n_traces)
        failure = None
        if not pairs:
            if self.timed_out:
                failure = "timeout", "subprocess.TimeoutExpired: no trace pairs and the fault hangs"
            else:
                pairs, failure = self._fallback_pairs()
        self.n_pairs = len(pairs)
        return failure or detectors.direct_leak(self.ctx.secret, self.ctx.eph_secrets, pairs)

    def _fallback_pairs(self):
        """(trace pairs, None), or (pairs so far, verdict) if a run fails."""
        pairs = []
        for k in range(FALLBACK_PAIRS):
            inputs = self.ctx.fallback_inputs(self.harness, k)
            try:
                self.skip.disarm()
                correct = self.harness.run(inputs, restore=True)
                self.skip.reset()
                pairs.append((correct, self.harness.run(inputs, restore=True)))
            except EmulationTimeout:
                return pairs, ("timeout", "subprocess.TimeoutExpired")
            except EmulationError as e:
                return pairs, ("error", f"trace collection failed: {e}")
        return pairs, None

    def counts(self):
        return {"fired": self.fired, "trials": self.trials, "sv_examined": self.examined,
                "sv_skipped": self.skipped, "crash": self.crashes, "timeout": int(self.timed_out)}
