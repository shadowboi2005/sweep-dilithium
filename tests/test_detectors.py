"""The three leakage tests on small cases with known verdicts."""
import unittest

import numpy as np
import support  # noqa: F401  (import path)

import detectors as T

SEEDS, EPH = [0, 1], 2
GRID = [(p, j) for p in SEEDS for j in range(EPH)]


def trials(correct, faulty):
    """The same words for every (p, r), or a function of (p, j)."""
    get = lambda x, k: np.array(x(*k) if callable(x) else x)  # noqa: E731
    return {k: (get(correct, k), get(faulty, k)) for k in GRID}


def sweep(diff_mode="xor", n_words=2):
    return T.EarlyStopSweep(SEEDS, EPH, n_words, diff_mode)


class IneffectiveTest(unittest.TestCase):
    def test_needs_an_ineffective_and_an_effective_value(self):
        s = sweep()
        s.feed(0, trials([1, 1], [1, 2]))
        self.assertIsNone(s.ineffective)
        s.feed(1, trials([1, 1], [3, 2]))
        pos, witnesses = s.ineffective
        self.assertEqual((pos, witnesses), (0, [(0, 1)] * len(SEEDS)))

    def test_every_seed_needs_the_pair(self):
        s = sweep()
        s.feed(0, trials([1, 1], [1, 2]))
        s.feed(1, trials([1, 1], lambda p, j: [3 if p == 0 else 1, 2]))
        self.assertIsNone(s.ineffective)

    def test_must_be_constant_over_ephemeral_draws(self):
        s = sweep()
        s.feed(0, trials([1, 1], [1, 2]))
        s.feed(1, trials([1, 1], lambda p, j: [3 if j == 0 else 1, 2]))
        self.assertIsNone(s.ineffective)


class CorrectionTest(unittest.TestCase):
    def test_constant_difference(self):
        s = sweep()
        s.feed(5, trials(lambda p, j: [p, 0], lambda p, j: [p ^ 7, 0]))
        self.assertEqual(s.correction, (0, 5, 7))

    def test_zero_difference_is_not_a_hit(self):
        s = sweep()
        s.feed(5, trials([1, 2], [1, 2]))
        self.assertIsNone(s.correction)

    def test_difference_varying_with_the_background_is_not_a_hit(self):
        s = sweep()
        s.feed(5, trials([0, 0], lambda p, j: [1 + p, 1 + j]))
        self.assertIsNone(s.correction)

    def test_mod_q_differences(self):
        s = sweep("mod-sub")
        s.feed(5, trials([0, 0], [1, 0]))
        self.assertEqual(s.correction, (0, 5, T.Q - 1))

    def test_stops_only_when_both_hit(self):
        s = sweep()
        self.assertFalse(s.feed(0, trials([1, 1], [1, 2])))
        self.assertTrue(s.feed(1, trials([1, 1], [3, 2])))


def trace(secret, correct, faulty):
    record = lambda out: {"inputs": {"s": bytes(secret)}, "outputs": {"o": bytes(out)}}  # noqa: E731
    return record(correct), record(faulty)


class DirectLeakTest(unittest.TestCase):
    def verdict(self, *traces):
        return T.direct_leak("s", [], list(traces))[0]

    def test_output_holds_the_secret(self):
        self.assertEqual(self.verdict(trace([1, 2], [9, 9], [1, 9]), trace([3, 4], [9, 9], [3, 9])), "yes")

    def test_one_counterexample_is_enough_to_reject(self):
        self.assertEqual(self.verdict(trace([1, 2], [9, 9], [1, 9]), trace([3, 4], [9, 9], [8, 9])), "no")

    def test_secret_must_vary(self):
        self.assertEqual(self.verdict(trace([1, 2], [9, 9], [1, 9]), trace([1, 4], [9, 9], [1, 9])), "no")

    def test_no_change_is_no_leak(self):
        self.assertEqual(self.verdict(trace([1], [9], [9])), "no")

    def test_secret_not_in_the_trace(self):
        self.assertEqual(T.direct_leak("x", [], [trace([1], [9], [1])])[0], "skip")


class TracePairsTest(unittest.TestCase):
    def test_round_robin_and_limit(self):
        dirs = {d: [(i, f"{d}c{i}", f"{d}f{i}") for i in range(10)] for d in ("", "seed1", "seed2")}
        pairs = T.trace_pairs(dirs, limit=7)
        self.assertEqual(len(pairs), 7)
        self.assertEqual([c.split("c")[0] for c, _ in pairs], ["", "seed1", "seed2"] * 2 + [""])
        self.assertNotEqual([c for c, _ in T.trace_pairs({"": dirs[""]}, 10)], [f"c{i}" for i in range(10)])


if __name__ == "__main__":
    unittest.main()
