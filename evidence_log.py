"""Evidence log: one CSV row per page attempted, plus a JSON manifest for the run.

The CSV is written row by row as the crawl progresses, so a crash mid-run still
leaves a log of everything done up to that point.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = [
    "seq",
    "status",               # captured | skipped | error
    "captured_at_local",    # ISO 8601 with UTC offset
    "captured_at_utc",
    "depth",
    "found_on",             # page the link was found on (blank for the start URL)
    "requested_url",
    "final_url",            # after redirects
    "http_status",
    "page_title",
    "png_file",
    "png_sha256",
    "pdf_file",
    "pdf_sha256",
    "drive_png_id",
    "drive_pdf_id",
    "drive_checksum_match", # Drive's MD5 of the uploaded files == local MD5
    "note",
]


def file_hash(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def timestamps() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.astimezone().isoformat(timespec="seconds"), now.isoformat(timespec="seconds")


class CaptureLog:
    def __init__(self, csv_path: Path, manifest_path: Path, run_info: dict):
        self.csv_path = csv_path
        self.manifest_path = manifest_path
        self.run_info = run_info
        self.entries: list[dict] = []
        self._fh = csv_path.open("w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=COLUMNS)
        self._writer.writeheader()
        self._fh.flush()

    def add(self, **fields) -> dict:
        local, utc = timestamps()
        entry = {col: "" for col in COLUMNS}
        entry.update(seq=len(self.entries) + 1, captured_at_local=local, captured_at_utc=utc)
        entry.update({k: v for k, v in fields.items() if v is not None})
        self.entries.append(entry)
        self._writer.writerow(entry)
        self._fh.flush()
        return entry

    def close(self, **summary) -> None:
        """Finish the CSV and write the manifest, including the CSV's own SHA-256."""
        self._fh.close()
        counts = {s: sum(e["status"] == s for e in self.entries) for s in ("captured", "skipped", "error")}
        manifest = {
            "run": {**self.run_info, "finished_at": timestamps()[0], **summary},
            "counts": counts,
            "log_csv": {"file": self.csv_path.name, "sha256": file_hash(self.csv_path)},
            "pages": self.entries,
        }
        self.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
