import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class E2eSafetyTests(unittest.TestCase):
    def test_live_mode_is_blocked_without_arm_or_credentials(self) -> None:
        env = {**os.environ, "BOT_MODE": "live", "BOT_ALLOW_LIVE": "false", "THREE_COMMAS_SECRET": "", "THREE_COMMAS_BOT_UUID": ""}
        completed = subprocess.run(
            [sys.executable, "-m", "daytrading_bot.cli", "live-scan", "--mode", "live", "--duration-seconds", "1", "--max-messages", "1"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "blocked")
        self.assertFalse(payload["preflight"]["armed"])
        self.assertIn("BOT_ALLOW_LIVE is false", payload["preflight"]["issues"])

    def test_e2e_verify_harness_runs_end_to_end(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "e2e_verify.py"), "--skip-unit"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)
        self.assertTrue(all(stage["ok"] for stage in payload["results"]))
        stage_names = {stage["name"] for stage in payload["results"]}
        self.assertIn("positive_backtest", stage_names)
        self.assertIn("live_block", stage_names)


if __name__ == "__main__":
    unittest.main()
