from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from openclaw_ephemeral.filesystem import atomic_write_json


class AtomicWriteTests(unittest.TestCase):
    def test_write_creates_parent_and_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "new" / "openclaw.json"
            atomic_write_json(destination, {"answer": 42})
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8")),
                {"answer": 42},
            )
            self.assertTrue(destination.read_bytes().endswith(b"\n"))

    def test_serialization_failure_preserves_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            destination = Path(raw) / "openclaw.json"
            destination.write_text('{"old": true}\n', encoding="utf-8")
            with self.assertRaises(TypeError):
                atomic_write_json(destination, {"invalid": object()})
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                '{"old": true}\n',
            )
            self.assertEqual(
                list(destination.parent.glob(f".{destination.name}.*.tmp")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
