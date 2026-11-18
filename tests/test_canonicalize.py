import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from data_record_server.canonicalize import canonicalize_jsonl_files


class CanonicalizeTest(unittest.TestCase):
    def test_sorts_records_deduplicates_exact_lines_and_quarantines_out_of_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "connection-1.jsonl"
            second = root / "connection-2.jsonl"
            duplicate = json.dumps({"gpsTime": "1000", "n": 1}, separators=(",", ":"))
            first.write_text(
                duplicate + "\n"
                + json.dumps({"gpsTime": "3000", "n": 3}) + "\n"
                + json.dumps({"gpsTime": "-100000", "n": 0}) + "\n",
                encoding="utf-8",
            )
            second.write_text(
                json.dumps({"gpsTime": "2000", "n": 2}) + "\n"
                + duplicate + "\n",
                encoding="utf-8",
            )
            output = root / "canonical.jsonl"
            quarantine = root / "quarantine.jsonl"

            result = canonicalize_jsonl_files(
                [first, second],
                output,
                quarantine,
                opened_at=datetime.fromtimestamp(0, timezone.utc),
                closed_at=datetime.fromtimestamp(4, timezone.utc),
                tolerance=timedelta(seconds=0),
            )

            self.assertEqual(3, result.record_count)
            self.assertEqual(1, result.quarantined_count)
            self.assertEqual(
                ["1000", "2000", "3000"],
                [json.loads(line)["gpsTime"] for line in output.read_text().splitlines()],
            )
            self.assertEqual(
                ["-100000"],
                [json.loads(line)["gpsTime"] for line in quarantine.read_text().splitlines()],
            )

    def test_rejects_invalid_json_line_into_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "connection.jsonl"
            source.write_text('{"gpsTime":"1000"}\nnot-json\n', encoding="utf-8")
            output = root / "canonical.jsonl"
            quarantine = root / "quarantine.jsonl"

            result = canonicalize_jsonl_files(
                [source],
                output,
                quarantine,
                opened_at=datetime.fromtimestamp(0, timezone.utc),
                closed_at=datetime.fromtimestamp(2, timezone.utc),
                tolerance=timedelta(seconds=0),
            )

            self.assertEqual(1, result.record_count)
            self.assertEqual(1, result.quarantined_count)
            self.assertTrue(output.exists())
            self.assertTrue(quarantine.exists())
