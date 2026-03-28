import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from daytrading_bot.config import load_config_from_env
from daytrading_bot.runtime_layout import build_runtime_paths, migrate_legacy_runtime, sanitize_device_id


class RuntimeLayoutTests(unittest.TestCase):
    def test_sanitize_device_id_normalizes_machine_names(self) -> None:
        self.assertEqual(sanitize_device_id("DESKTOP-01"), "desktop-01")
        self.assertEqual(sanitize_device_id("My Laptop (Dev)"), "my-laptop-dev")

    def test_build_runtime_paths_uses_project_root_and_device(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            paths = build_runtime_paths(project_root=tempdir, device_id="Laptop Main")
            self.assertEqual(paths.device_id, "laptop-main")
            self.assertTrue(paths.data_dir.endswith(".runtime\\laptop-main\\data") or paths.data_dir.endswith(".runtime/laptop-main/data"))
            self.assertTrue(paths.telemetry_path.endswith("trading_events.jsonl"))

    def test_migrate_runtime_layout_copies_legacy_data_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "data").mkdir()
            (root / "logs").mkdir()
            (root / "data" / "XBTEUR.csv").write_text("ts,open,high,low,close,volume\n", encoding="utf-8")
            (root / "logs" / "trading_events.jsonl").write_text("{}", encoding="utf-8")

            report = migrate_legacy_runtime(project_root=root, device_id="desktop-main", copy_only=True)

            runtime_root = root / ".runtime" / "desktop-main"
            self.assertTrue((runtime_root / "data" / "XBTEUR.csv").exists())
            self.assertTrue((runtime_root / "logs" / "trading_events.jsonl").exists())
            self.assertEqual(report["device_id"], "desktop-main")

    def test_load_config_from_env_uses_device_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir, patch.dict(
            "os.environ",
            {
                "FLOW_BOT_DEVICE_ID": "laptop-main",
                "BOT_MODE": "paper",
            },
            clear=False,
        ):
            bot_config, _ = load_config_from_env(project_root=tempdir)

            self.assertIn(".runtime", bot_config.telemetry_path)
            self.assertIn("laptop-main", bot_config.telemetry_path)
            self.assertIn("laptop-main", bot_config.strategy_lab_state_path)


if __name__ == "__main__":
    unittest.main()
