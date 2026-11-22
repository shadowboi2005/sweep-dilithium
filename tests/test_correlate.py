"""Joining machine-level verdicts with the IR report."""
import csv
import os
import tempfile
import unittest

from support import ROOT

import correlate


class CorrelateTest(unittest.TestCase):
    def test_every_report_row_parses(self):
        rows = correlate.load_report(os.path.join(ROOT, "data", "fault_report_dilithium.csv"))
        self.assertEqual(len(rows), 317)
        self.assertEqual({r["detected"] for r in rows} - {"yes", "no", "timeout", "skip", "error"}, set())

    def test_parse_fault(self):
        fn = "pqcrystals_dilithium2_ref_poly_add"
        self.assertEqual(correlate.parse_fault(f"binOpFault/{fn}_line8_opA", fn), ("binOpFault", 8, "opA", ""))
        self.assertEqual(correlate.parse_fault(f"loopOrFuncSkip/{fn}_fnSkip_foo_bar_line12", fn),
                         ("loopOrFuncSkip", 12, "fnSkip", "foo_bar"))
        with self.assertRaises(ValueError):
            correlate.parse_fault(f"other/{fn}_line8_opA", fn)

    def test_undecided_verdicts_are_not_a_no(self):
        f = {"fired": "4", "sv_examined": "0", "ineffective": "no", "direct_leak": "error"}
        self.assertFalse(correlate._decided(f, "ineffective"))  # every trial crashed
        self.assertFalse(correlate._decided(f, "direct_leak"))
        self.assertTrue(correlate._decided({**f, "sv_examined": "3"}, "ineffective"))
        self.assertFalse(correlate._decided({**f, "sv_examined": "3", "fired": "0"}, "ineffective"))

    def test_score(self):
        s = correlate._score([("yes", "yes")] * 2 + [("yes", "no")] + [("no", "yes")] + [("no", "no")] * 6)
        self.assertEqual((s["n"], s["agree"], s["always_no"]), (10, 8, 7))
        self.assertAlmostEqual(s["kappa"], (0.8 - 0.58) / 0.42, places=3)

    def test_published_report_rows_follow_from_the_sweep(self):
        for build in ("O0", "Os"):
            d = os.path.join(ROOT, "results", build)
            with tempfile.TemporaryDirectory() as out:
                correlate.main(["map", "--faults", os.path.join(d, "machine_faults.csv"),
                                "--elf", os.path.join(ROOT, "elf", f"dil2_{build}.elf"),
                                "--ir", os.path.join(ROOT, "elf", "dil2.relabel.ll"),
                                "--report", os.path.join(ROOT, "data", "fault_report_dilithium.csv"), "--out", out])
                with open(os.path.join(out, "report_rows.csv"), newline="") as f:
                    rows = list(csv.DictReader(f))
            with open(os.path.join(d, "report_rows.csv"), newline="") as f:
                published = list(csv.DictReader(f))
            for column in ("fault", "test", "pcs", "n_pcs", "n_inlined_pcs", "mc_any", "agree"):
                self.assertEqual([r[column] for r in rows], [r[column] for r in published], (build, column))
            # copies inlined elsewhere never decide a row
            self.assertTrue(all(r["mc_any"] != "yes" for r in rows if r["n_pcs"] == "0"))


if __name__ == "__main__":
    unittest.main()
