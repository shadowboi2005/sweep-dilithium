"""find_faults end to end on one small function."""
import csv
import json
import os
import tempfile
import unittest

from support import ROOT

import find_faults

FN = "pqcrystals_dilithium2_ref_poly_sub"


def sweep(out, *extra, jobs=2):
    find_faults.main(["--elf", os.path.join(ROOT, "elf", "dil2_Os.elf"), "--ir", os.path.join(ROOT, "elf", "dil2.relabel.ll"),
                      "--out", out, "--functions", "poly_sub", "--seeds", "0", "1", "--field-mod", "4",
                      "--jobs", str(jobs), *extra])
    with open(os.path.join(out, "machine_faults.csv"), newline="") as f:
        return [{k: v for k, v in r.items() if k not in ("seconds", "cpu_seconds")} for r in csv.DictReader(f)]


class SweepTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = os.path.join(cls.tmp.name, "a")
        cls.rows = sweep(cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_skipped_subtraction_leaks_the_secret(self):
        row = next(r for r in self.rows if r["pc"] == "0x1870")
        self.assertEqual((row["asm"], row["direct_leak"]), ("sub.w r3, ip, r3", "yes"))

    def test_does_not_depend_on_the_number_of_workers(self):
        self.assertEqual(sweep(os.path.join(self.tmp.name, "b"), jobs=1), self.rows)

    def test_resume_reuses_rows_and_keeps_golden_time(self):
        before = sum(1 for _ in open(os.path.join(self.out, "checkpoint.jsonl")))
        self.assertEqual(sweep(self.out), self.rows)
        with open(os.path.join(self.out, "checkpoint.jsonl")) as f:
            added = [line for line in f.read().splitlines()[before:] if line.strip()]
        self.assertEqual(added, [])
        with open(os.path.join(self.out, "timing_dilithium.csv"), newline="") as f:
            self.assertGreater(float(next(csv.DictReader(f))["golden_seconds"]), 0)

    def test_changed_parameters_are_not_resumed(self):
        out = os.path.join(self.tmp.name, "c")
        sweep(out)
        rows = sweep(out, "--trial-timeout", "299")
        self.assertEqual(rows, self.rows)
        keys = {json.loads(line)["key"] for line in open(os.path.join(out, "checkpoint.jsonl")) if line.strip()}
        self.assertEqual(len(keys), 2)

    def test_secret_that_never_reaches_the_function_is_an_error(self):
        out = os.path.join(self.tmp.name, "d")
        find_faults.main(["--elf", os.path.join(ROOT, "elf", "dil2_Os.elf"), "--ir",
                          os.path.join(ROOT, "elf", "dil2.relabel.ll"), "--out", out, "--functions", "open", "--jobs", "1"])
        with open(os.path.join(out, "timing_dilithium.csv"), newline="") as f:
            self.assertIn("active length 0", next(csv.DictReader(f))["status"])


if __name__ == "__main__":
    unittest.main()
