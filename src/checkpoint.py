"""Finished work of a sweep, appended to a file so that a rerun resumes."""
import json


class Checkpoint:
    """Records written under a different `key` are ignored."""

    def __init__(self, path, key):
        self.key = key
        self.rows = {}  # (function, pc) -> row
        self.golden_seconds = {}  # function -> [wall, cpu] of its unfaulted runs
        self._load(path)
        self._file = open(path, "a")
        self._file.write("\n")  # ends a line cut off by a kill

    def _load(self, path):
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("key") != self.key:
                continue
            if "row" in record:
                self._keep(record["row"])
            else:
                self.golden_seconds.update(record["golden"])

    def _keep(self, row):
        self.rows[row["function"], row["pc"]] = row

    def _append(self, **record):
        self._file.write(json.dumps({"key": self.key, **record}) + "\n")
        self._file.flush()

    def add_row(self, row):
        self._keep(row)
        self._append(row=row)

    def add_golden_seconds(self, seconds):
        self.golden_seconds.update(seconds)
        self._append(golden=self.golden_seconds)

    def close(self):
        self._file.close()
