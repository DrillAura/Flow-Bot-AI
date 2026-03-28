import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desktop_dashboard_launcher import is_dashboard_alive, is_port_free, resolve_dashboard_port, resolve_project_root


class DesktopDashboardLauncherTests(unittest.TestCase):
    def test_resolve_project_root_prefers_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            with patch.dict("os.environ", {"FLOWBOT_PROJECT_ROOT": tempdir}, clear=False):
                resolved = resolve_project_root()
        self.assertEqual(resolved, Path(tempdir).resolve())

    def test_resolve_project_root_uses_config_file_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            config_path = Path(__import__("desktop_dashboard_launcher").__file__).resolve().with_name("desktop_dashboard.json")
            original = config_path.read_text(encoding="utf-8") if config_path.exists() else None
            try:
                config_path.write_text(json.dumps({"project_root": str(temp_path)}), encoding="utf-8")
                with patch.dict("os.environ", {}, clear=True):
                    resolved = resolve_project_root()
            finally:
                if original is None:
                    config_path.unlink(missing_ok=True)
                else:
                    config_path.write_text(original, encoding="utf-8")
        self.assertEqual(resolved, temp_path.resolve())

    def test_resolve_dashboard_port_reuses_existing_dashboard(self) -> None:
        with patch("desktop_dashboard_launcher.is_dashboard_alive", side_effect=lambda host, port: port == 8787), patch("desktop_dashboard_launcher.is_port_free", return_value=False):
            port, reused = resolve_dashboard_port("127.0.0.1", 8787)
        self.assertEqual(port, 8787)
        self.assertTrue(reused)

    def test_resolve_dashboard_port_finds_next_free_port(self) -> None:
        with patch("desktop_dashboard_launcher.is_dashboard_alive", return_value=False), patch("desktop_dashboard_launcher.is_port_free", side_effect=lambda host, port: port == 8788):
            port, reused = resolve_dashboard_port("127.0.0.1", 8787)
        self.assertEqual(port, 8788)
        self.assertFalse(reused)


if __name__ == "__main__":
    unittest.main()
