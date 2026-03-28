import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from daytrading_bot.dashboard import load_supervisor_state_payload, write_supervisor_dashboard


class DashboardTests(unittest.TestCase):
    def test_dashboard_loader_backfills_missing_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_path = Path(tempdir) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "status": "waiting_for_history",
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "history_progress": {
                            "required_days": 13,
                            "available_days": 1.5,
                            "remaining_days": 11.5,
                            "progress_pct": 11.5,
                        },
                    }
                ),
                encoding="utf-8",
            )
            payload = load_supervisor_state_payload(state_path)

        self.assertIn("daily_summary", payload)
        self.assertEqual(payload["daily_summary"]["supervisor_status"], "waiting_for_history")

    def test_dashboard_writer_outputs_html_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output_path = Path(tempdir) / "dashboard.html"
            write_supervisor_dashboard(
                output_path,
                {
                    "status": "waiting_for_history",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "dashboard_path": str(output_path),
                    "daily_summary": {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "supervisor_status": "waiting_for_history",
                        "progress_pct": 11.5,
                        "available_days": 1.5,
                        "required_days": 13,
                        "eta": None,
                        "last_errors": ["temporary ssl timeout"],
                        "gate_status": "waiting_for_history",
                        "gate_ready": False,
                        "gate_blockers": ["local_oos_history_not_ready"],
                        "paper_forward_status": "idle",
                    },
                    "history_progress": {
                        "required_days": 13,
                        "available_days": 1.5,
                        "remaining_days": 11.5,
                        "progress_pct": 11.5,
                        "estimated_ready_at": None,
                    },
                    "last_prepare_report": {
                        "capture_report": {
                            "final_history_status": {
                                "pair_status": {
                                    "XBTEUR": {
                                        "candles_1m": 100,
                                        "candles_15m": 10,
                                        "span_days": 1.5,
                                        "last_ts": "2026-03-23T20:46:00+00:00",
                                    }
                                }
                            }
                        }
                    },
                },
                refresh_seconds=15,
            )
            html = output_path.read_text(encoding="utf-8")

        self.assertIn("Supervisor Dashboard", html)
        self.assertIn("temporary ssl timeout", html)
        self.assertIn("XBTEUR", html)


if __name__ == "__main__":
    unittest.main()
