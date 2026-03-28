import tempfile
import unittest
from pathlib import Path

from daytrading_bot.device_bootstrap import bootstrap_device, create_device_desktop_launchers


class DeviceBootstrapTests(unittest.TestCase):
    def test_create_device_desktop_launchers_writes_three_cmd_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            desktop = Path(tempdir) / "Desktop"
            launchers = create_device_desktop_launchers(
                project_root=tempdir,
                device_id="laptop-main",
                desktop_dir=desktop,
            )

            self.assertEqual(len(launchers), 3)
            for launcher in launchers:
                self.assertTrue(Path(launcher).exists())

    def test_bootstrap_device_can_migrate_legacy_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "data").mkdir()
            (root / "logs").mkdir()
            (root / "data" / "XBTEUR.csv").write_text("ts,open,high,low,close,volume\n", encoding="utf-8")
            desktop = root / "Desktop"

            report = bootstrap_device(
                project_root=root,
                device_id="desktop-main",
                desktop_dir=desktop,
                migrate_legacy=True,
                move_legacy=False,
            )

            self.assertEqual(report.device_id, "desktop-main")
            self.assertEqual(len(report.created_launchers), 3)
            self.assertIsNotNone(report.migration)
            self.assertTrue((root / ".runtime" / "desktop-main" / "data" / "XBTEUR.csv").exists())


if __name__ == "__main__":
    unittest.main()
