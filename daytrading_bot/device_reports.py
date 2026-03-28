from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_config_from_env
from .reporting import run_forward_test_report
from .runtime_layout import RuntimePaths, build_runtime_paths, ensure_runtime_dirs
from .workflows import run_history_status, run_monitor_supervisor


@dataclass(frozen=True)
class DeviceReport:
    generated_at: str
    device_id: str
    runtime_paths: RuntimePaths
    state_path: str | None
    monitor_status: str
    gate_status: str
    paper_forward_status: str
    history_available_days: float
    history_required_days: int
    sufficient_history: bool
    pair_count: int
    strategy_lab_champion: str | None
    strategy_lab_promotion_reason: str | None
    forward_closed_trades: int
    forward_win_rate: float
    forward_profit_factor: float
    forward_max_drawdown_pct: float
    forward_net_pnl_eur: float
    go_live_ready: bool
    summary_markdown_path: str
    summary_json_path: str


def find_latest_runtime_state(ops_logs_dir: Path) -> Path | None:
    candidates = list(ops_logs_dir.glob("**/supervisor_state.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def build_device_report(project_root: str | Path | None = None, device_id: str | None = None) -> DeviceReport:
    runtime_paths = ensure_runtime_dirs(build_runtime_paths(project_root=project_root, device_id=device_id))
    bot_config, _ = load_config_from_env(project_root=runtime_paths.project_root, device_id=runtime_paths.device_id)
    latest_state_path = find_latest_runtime_state(Path(runtime_paths.ops_logs_dir))
    monitor = asdict(run_monitor_supervisor(latest_state_path)) if latest_state_path else {
        "status": "missing",
        "daily_summary": None,
    }
    history = asdict(
        run_history_status(
            Path(runtime_paths.data_dir),
            bot_config,
            train_days=10,
            test_days=3,
        )
    )
    forward = asdict(run_forward_test_report(Path(bot_config.telemetry_path), bot_config))
    daily_summary = monitor.get("daily_summary") or {}
    strategy_lab = monitor.get("strategy_lab") or {}
    reports_root = Path(runtime_paths.project_root) / "reports" / "devices" / runtime_paths.device_id
    reports_root.mkdir(parents=True, exist_ok=True)
    json_path = reports_root / "latest.json"
    markdown_path = reports_root / "latest.md"
    report = DeviceReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        device_id=runtime_paths.device_id,
        runtime_paths=runtime_paths,
        state_path=str(latest_state_path) if latest_state_path else None,
        monitor_status=str(monitor.get("status") or "missing"),
        gate_status=str(daily_summary.get("gate_status") or "unknown"),
        paper_forward_status=str(daily_summary.get("paper_forward_status") or "idle"),
        history_available_days=float(history.get("available_days") or 0.0),
        history_required_days=int(history.get("required_days") or 13),
        sufficient_history=bool(history.get("sufficient_history")),
        pair_count=len(history.get("pair_status") or {}),
        strategy_lab_champion=str(strategy_lab.get("current_paper_strategy_id") or "") or None,
        strategy_lab_promotion_reason=str(strategy_lab.get("promotion_reason") or "") or None,
        forward_closed_trades=int(forward.get("closed_trades") or 0),
        forward_win_rate=float(forward.get("win_rate") or 0.0),
        forward_profit_factor=float(forward.get("profit_factor") or 0.0),
        forward_max_drawdown_pct=float(forward.get("max_drawdown_pct") or 0.0),
        forward_net_pnl_eur=float(forward.get("net_pnl_eur") or 0.0),
        go_live_ready=bool(forward.get("go_live_ready")),
        summary_markdown_path=str(markdown_path),
        summary_json_path=str(json_path),
    )
    return report


def export_device_report(project_root: str | Path | None = None, device_id: str | None = None) -> dict[str, Any]:
    report = build_device_report(project_root=project_root, device_id=device_id)
    payload = asdict(report)
    summary_json_path = Path(report.summary_json_path)
    summary_markdown_path = Path(report.summary_markdown_path)
    summary_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    summary_markdown_path.write_text(render_device_report_markdown(report), encoding="utf-8")
    return payload


def render_device_report_markdown(report: DeviceReport) -> str:
    history_pct = 0.0
    if report.history_required_days > 0:
        history_pct = (report.history_available_days / report.history_required_days) * 100.0
    lines = [
        f"# Device Report: {report.device_id}",
        "",
        f"- Generated: `{report.generated_at}`",
        f"- Monitor status: `{report.monitor_status}`",
        f"- Gate status: `{report.gate_status}`",
        f"- Paper forward: `{report.paper_forward_status}`",
        f"- History: `{report.history_available_days:.3f} / {report.history_required_days}` days (`{history_pct:.2f}%`)",
        f"- Pairs tracked: `{report.pair_count}`",
        f"- Champion: `{report.strategy_lab_champion or 'n/a'}`",
        f"- Promotion reason: `{report.strategy_lab_promotion_reason or 'n/a'}`",
        f"- Forward trades: `{report.forward_closed_trades}`",
        f"- Forward win rate: `{report.forward_win_rate:.4f}`",
        f"- Forward profit factor: `{report.forward_profit_factor:.4f}`",
        f"- Forward max drawdown: `{report.forward_max_drawdown_pct:.4f}`",
        f"- Forward net PnL: `{report.forward_net_pnl_eur:.4f} EUR`",
        f"- Go-live ready: `{str(report.go_live_ready).lower()}`",
        "",
        "## Runtime",
        "",
        f"- Runtime root: `{report.runtime_paths.runtime_root}`",
        f"- Data dir: `{report.runtime_paths.data_dir}`",
        f"- Logs dir: `{report.runtime_paths.logs_dir}`",
        f"- Latest state: `{report.state_path or 'missing'}`",
    ]
    return "\n".join(lines) + "\n"
