import json
import tempfile
import unittest
from pathlib import Path

from daytrading_bot.device_reports import export_device_report


class DeviceReportsTests(unittest.TestCase):
    def test_export_device_report_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            payload = export_device_report(project_root=tempdir, device_id="laptop-main")

            json_path = Path(payload["summary_json_path"])
            markdown_path = Path(payload["summary_markdown_path"])

            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            self.assertEqual(payload["device_id"], "laptop-main")

            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["device_id"], "laptop-main")
            self.assertIn("history_available_days", loaded)
            self.assertIn("monitor_status", loaded)


if __name__ == "__main__":
    unittest.main()
