#!/usr/bin/env python3
"""Re-hash every capture in a folder and compare against its capture log.

Usage: python verify.py <folder containing capture-log__*.csv>
Exit code 0 = every file matches its logged SHA-256; 1 = something is missing or altered.
"""

import csv
import sys
from pathlib import Path

from evidence_log import file_hash


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    logs = sorted(folder.glob("capture-log__*.csv"))
    if not logs:
        print(f"No capture-log__*.csv found in {folder}")
        return 1
    ok = bad = 0
    for log in logs:
        print(f"== {log.name}")
        with log.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                for kind in ("png", "pdf"):
                    name, expected = row[f"{kind}_file"], row[f"{kind}_sha256"]
                    if not name:
                        continue
                    path = folder / name
                    if not path.exists():
                        print(f"  MISSING  {name}")
                        bad += 1
                    elif file_hash(path) != expected:
                        print(f"  ALTERED  {name}")
                        bad += 1
                    else:
                        ok += 1
    print(f"\n{ok} files verified, {bad} problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
