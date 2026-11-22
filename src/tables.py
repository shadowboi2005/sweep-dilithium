"""CSV files as lists of dicts."""
import csv
import os


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields=None):
    """`fields` defaults to the rows' keys in order of first appearance."""
    if fields is None:
        fields = list(dict.fromkeys(k for r in rows for k in r))
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
