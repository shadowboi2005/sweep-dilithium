"""Samplers, secret values and argument layouts."""
import json
import os
import random
import unittest

from support import ROOT

import ir
import qemu_inputs as QI

ETA = "uniform in [-eta, eta]"
MOD_Q = "uniform polynomial in R_q"
LAYOUTS = os.path.join(ROOT, "results", "O0", "layouts.json")


class DomainTest(unittest.TestCase):
    def test_small_domain_is_complete_with_zero_first(self):
        self.assertEqual(QI.domain_values(ETA, 256), [0, -2, -1, 1, 2])

    def test_large_domain_is_spread_over_the_range(self):
        v = QI.domain_values(MOD_Q, 256)
        self.assertEqual((len(v), len(set(v)), v[0]), (256, 256, 0))
        self.assertEqual(max(v), QI.Q - 1)

    def test_bytes(self):
        self.assertEqual(QI.domain_values(None, 256), list(range(256)))
        self.assertEqual(QI.infer_field_mod(None), 256)


class SamplerTest(unittest.TestCase):
    def test_coefficients_stay_in_their_domain(self):
        for dist in (ETA, MOD_Q):
            lo, hi = QI.domain_bounds(dist)
            raw = bytes(b & 0xFF for b in QI.sample_for_distribution(dist, 1024, random.Random(1), 256))
            values = [int.from_bytes(raw[i:i + 4], "little", signed=True) for i in range(0, 1024, 4)]
            self.assertTrue(all(lo <= v <= hi for v in values), dist)

    def test_is_reproducible(self):
        draw = lambda seed: QI.sample_for_distribution(MOD_Q, 64, random.Random(seed), 256)  # noqa: E731
        self.assertEqual(draw(3), draw(3))
        self.assertNotEqual(draw(3), draw(4))


class OverrideTest(unittest.TestCase):
    def test_whole_coefficient(self):
        vals = QI.apply_override({"distribution": ETA}, [9] * 8, 5, -2)
        self.assertEqual([v & 0xFF for v in vals], [9] * 4 + [0xFE, 0xFF, 0xFF, 0xFF])

    def test_one_byte(self):
        self.assertEqual(QI.apply_override({}, [9] * 4, 1, 0x1FF), [9, 0xFF, 9, 9])

    def test_outside_the_buffer(self):
        self.assertEqual(QI.apply_override({}, [9, 9], 2, 1), [9, 9])


class LayoutTest(unittest.TestCase):
    def test_layouts_match_the_published_run(self):
        with open(os.path.join(ROOT, "elf", "dil2.relabel.ll")) as f:
            layouts = QI.IRLayout(ir.parse_module(f.read()))
        with open(LAYOUTS) as f:
            published = json.load(f)
        self.assertEqual(len(published), 31)
        for fn, info in published.items():
            layout = layouts.build(fn, QI.load_sample(os.path.join(ROOT, "data", "function_inputs"), fn))
            self.assertEqual(json.loads(json.dumps(layout, default=str)), info["layout"], fn)


if __name__ == "__main__":
    unittest.main()
