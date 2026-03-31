from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from time import sleep

from .backtest import CsvBacktester
from .calibration import run_calibration
from .config import load_config_from_env
from .device_bootstrap import bootstrap_device_payload
from .device_reports import export_device_report
from .dashboard import load_supervisor_state_payload, write_supervisor_dashboard
from .dashboard_app import serve_dashboard_app
from .diagnostics import run_signal_diagnostics
from .execution import ThreeCommasSignalClient
from .history import load_local_histories
from .kraken import KrakenPublicClient
from .live import run_live_scanner
from .models import ActiveTrade, DayTradeIntent
from .personal_journal import append_personal_trade, build_personal_trade_entry, ensure_personal_journal_path, run_personal_journal_report
from .research import run_walk_forward, run_walk_forward_optimization
from .reporting import run_forward_test_report, run_signal_debug_report
from .runtime_layout import build_runtime_paths, migrate_legacy_runtime
from .storage import history_csv_path
from .workflows import (
    run_ensure_supervisor,
    run_monitor_supervisor,
    run_history_status,
    run_paper_forward_gate,
    run_prepare_paper_forward,
    run_paper_forward_supervisor,
    run_stop_runtime,
    run_supervisor_watchdog,
    run_sync_history,
    run_sync_history_until_ready,
)


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _emit_json(payload) -> None:
    print(json.dumps(payload, indent=2, default=_json_default))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Day-trading bot utility CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sample_entry = sub.add_parser("sample-entry", help="Print a sample 3Commas entry payload")
    sample_entry.add_argument("--pair", required=True)
    sample_entry.add_argument("--price", required=True, type=float)
    sample_entry.add_argument("--stop", required=True, type=float)
    sample_entry.add_argument("--budget", required=True, type=float)

    sample_exit = sub.add_parser("sample-exit", help="Print a sample 3Commas exit payload")
    sample_exit.add_argument("--pair", required=True)
    sample_exit.add_argument("--price", required=True, type=float)
    sample_exit.add_argument("--entry", required=True, type=float)
    sample_exit.add_argument("--budget", required=True, type=float)

    backtest = sub.add_parser("backtest", help="Run CSV backtest")
    backtest.add_argument("--data-dir", required=True)

    download = sub.add_parser("download-ohlc", help="Download Kraken OHLC CSVs for all configured pairs")
    download.add_argument("--data-dir", required=True)
    download.add_argument("--interval", type=int, default=1)
    download.add_argument("--since", type=int, default=None)

    sync = sub.add_parser("sync-ohlc", help="Merge the latest Kraken OHLC snapshot into local CSV history")
    sync.add_argument("--data-dir", required=True)
    sync.add_argument("--interval", type=int, default=1)

    calibrate = sub.add_parser("calibrate", help="Run a small parameter sweep against local CSV history")
    calibrate.add_argument("--data-dir", required=True)
    calibrate.add_argument("--top", type=int, default=10)
    calibrate.add_argument("--profile", choices=["fast", "full"], default="fast")
    calibrate.add_argument("--setup", choices=["breakout", "recovery", "both"], default="recovery")
    calibrate.add_argument("--objective", choices=["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"], default="hybrid")

    walk_forward = sub.add_parser("walk-forward", help="Run anchored walk-forward research against local CSV history")
    walk_forward.add_argument("--data-dir", required=True)
    walk_forward.add_argument("--setup", choices=["breakout", "recovery", "both"], default="both")
    walk_forward.add_argument("--profile", choices=["fast", "full"], default="fast")
    walk_forward.add_argument("--objective", choices=["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"], default="hybrid")
    walk_forward.add_argument("--train-days", type=int, default=10)
    walk_forward.add_argument("--test-days", type=int, default=3)
    walk_forward.add_argument("--step-days", type=int, default=None)
    walk_forward.add_argument("--top", type=int, default=3)

    walk_forward_optimize = sub.add_parser("walk-forward-optimize", help="Rank parameter variants by aggregated OOS walk-forward performance")
    walk_forward_optimize.add_argument("--data-dir", required=True)
    walk_forward_optimize.add_argument("--setup", choices=["breakout", "recovery", "both"], default="both")
    walk_forward_optimize.add_argument("--profile", choices=["fast", "full"], default="fast")
    walk_forward_optimize.add_argument("--objective", choices=["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"], default="hybrid")
    walk_forward_optimize.add_argument("--train-days", type=int, default=10)
    walk_forward_optimize.add_argument("--test-days", type=int, default=3)
    walk_forward_optimize.add_argument("--step-days", type=int, default=None)
    walk_forward_optimize.add_argument("--top", type=int, default=5)

    diagnose = sub.add_parser("diagnose-signals", help="Explain why the current strategy does or does not emit setups")
    diagnose.add_argument("--data-dir", required=True)

    debug_signals = sub.add_parser("debug-signals", help="Show first-failure buckets per pair and session")
    debug_signals.add_argument("--data-dir", required=True)

    sync_loop = sub.add_parser("sync-ohlc-loop", help="Run repeated OHLC sync cycles against Kraken")
    sync_loop.add_argument("--data-dir", required=True)
    sync_loop.add_argument("--interval", type=int, default=1)
    sync_loop.add_argument("--cycles", type=int, default=5)
    sync_loop.add_argument("--sleep-seconds", type=int, default=60)

    sync_history = sub.add_parser("sync-history", help="Sync both 1m and 15m local history in one command")
    sync_history.add_argument("--data-dir", required=True)
    sync_history.add_argument("--cycles", type=int, default=1)
    sync_history.add_argument("--sleep-seconds", type=int, default=0)

    capture_until_ready = sub.add_parser("capture-until-ready", help="Keep syncing history until the requested OOS window is fully available")
    capture_until_ready.add_argument("--data-dir", required=True)
    capture_until_ready.add_argument("--train-days", type=int, default=10)
    capture_until_ready.add_argument("--test-days", type=int, default=3)
    capture_until_ready.add_argument("--poll-seconds", type=int, default=60)
    capture_until_ready.add_argument("--max-cycles", type=int, default=0)
    capture_until_ready.add_argument("--max-consecutive-errors", type=int, default=5)

    history_status = sub.add_parser("history-status", help="Show whether local history is sufficient for walk-forward")
    history_status.add_argument("--data-dir", required=True)
    history_status.add_argument("--train-days", type=int, default=10)
    history_status.add_argument("--test-days", type=int, default=3)

    live_scan = sub.add_parser("live-scan", help="Run live Kraken websocket scanner in dry-run mode")
    live_scan.add_argument("--available-eur", type=float, default=100.0)
    live_scan.add_argument("--duration-seconds", type=int, default=30)
    live_scan.add_argument("--max-messages", type=int, default=None)
    live_scan.add_argument("--bootstrap-dir", default="data")
    live_scan.add_argument("--mode", choices=["paper", "live"], default="paper")
    live_scan.add_argument("--stop-file", default=None)

    forward_report = sub.add_parser("forward-report", help="Summarize telemetry and evaluate go-live gates")
    forward_report.add_argument("--telemetry-path", default=None)

    paper_forward_gate = sub.add_parser("paper-forward-gate", help="Run E2E + history + walk-forward gate before starting a new paper-forward test")
    paper_forward_gate.add_argument("--data-dir", required=True)
    paper_forward_gate.add_argument("--telemetry-path", default=None)
    paper_forward_gate.add_argument("--setup", choices=["breakout", "recovery", "both"], default="both")
    paper_forward_gate.add_argument("--profile", choices=["fast", "full"], default="fast")
    paper_forward_gate.add_argument("--objective", choices=["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"], default="hybrid")
    paper_forward_gate.add_argument("--train-days", type=int, default=10)
    paper_forward_gate.add_argument("--test-days", type=int, default=3)
    paper_forward_gate.add_argument("--step-days", type=int, default=None)
    paper_forward_gate.add_argument("--top", type=int, default=3)
    paper_forward_gate.add_argument("--skip-e2e-unit", action="store_true")

    prepare_paper_forward = sub.add_parser("prepare-paper-forward", help="Capture history until ready, run OOS optimization, then evaluate the paper-forward release gate")
    prepare_paper_forward.add_argument("--data-dir", required=True)
    prepare_paper_forward.add_argument("--telemetry-path", default=None)
    prepare_paper_forward.add_argument("--setup", choices=["breakout", "recovery", "both"], default="both")
    prepare_paper_forward.add_argument("--profile", choices=["fast", "full"], default="fast")
    prepare_paper_forward.add_argument("--objective", choices=["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"], default="hybrid")
    prepare_paper_forward.add_argument("--train-days", type=int, default=10)
    prepare_paper_forward.add_argument("--test-days", type=int, default=3)
    prepare_paper_forward.add_argument("--step-days", type=int, default=None)
    prepare_paper_forward.add_argument("--top", type=int, default=3)
    prepare_paper_forward.add_argument("--poll-seconds", type=int, default=60)
    prepare_paper_forward.add_argument("--max-cycles", type=int, default=0)
    prepare_paper_forward.add_argument("--max-consecutive-errors", type=int, default=5)
    prepare_paper_forward.add_argument("--skip-e2e-unit", action="store_true")

    paper_forward_supervisor = sub.add_parser("paper-forward-supervisor", help="Long-running supervisor that collects history, waits for OOS readiness, evaluates the gate, and starts the next paper-forward run only if green")
    paper_forward_supervisor.add_argument("--data-dir", required=True)
    paper_forward_supervisor.add_argument("--telemetry-path", default=None)
    paper_forward_supervisor.add_argument("--setup", choices=["breakout", "recovery", "both"], default="both")
    paper_forward_supervisor.add_argument("--profile", choices=["fast", "full"], default="fast")
    paper_forward_supervisor.add_argument("--objective", choices=["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"], default="hybrid")
    paper_forward_supervisor.add_argument("--train-days", type=int, default=10)
    paper_forward_supervisor.add_argument("--test-days", type=int, default=3)
    paper_forward_supervisor.add_argument("--step-days", type=int, default=None)
    paper_forward_supervisor.add_argument("--top", type=int, default=3)
    paper_forward_supervisor.add_argument("--capture-poll-seconds", type=int, default=60)
    paper_forward_supervisor.add_argument("--supervisor-poll-seconds", type=int, default=300)
    paper_forward_supervisor.add_argument("--max-supervisor-cycles", type=int, default=0)
    paper_forward_supervisor.add_argument("--max-consecutive-errors", type=int, default=5)
    paper_forward_supervisor.add_argument("--paper-forward-available-eur", type=float, default=None)
    paper_forward_supervisor.add_argument("--paper-forward-duration-seconds", type=int, default=0)
    paper_forward_supervisor.add_argument("--enable-research-scans", action="store_true")
    paper_forward_supervisor.add_argument("--research-scan-available-eur", type=float, default=None)
    paper_forward_supervisor.add_argument("--research-scan-duration-seconds", type=int, default=90)
    paper_forward_supervisor.add_argument("--research-scan-max-messages", type=int, default=0)
    paper_forward_supervisor.add_argument("--research-scan-min-interval-seconds", type=int, default=900)
    paper_forward_supervisor.add_argument("--state-path", default=None)
    paper_forward_supervisor.add_argument("--paper-forward-stdout-path", default=None)
    paper_forward_supervisor.add_argument("--paper-forward-stderr-path", default=None)
    paper_forward_supervisor.add_argument("--skip-e2e-unit", action="store_true")

    ensure_supervisor = sub.add_parser("ensure-supervisor", help="One-shot check that restarts the supervisor if it is missing or dead")
    ensure_supervisor.add_argument("--data-dir", required=True)
    ensure_supervisor.add_argument("--telemetry-path", default=None)
    ensure_supervisor.add_argument("--setup", choices=["breakout", "recovery", "both"], default="both")
    ensure_supervisor.add_argument("--profile", choices=["fast", "full"], default="fast")
    ensure_supervisor.add_argument("--objective", choices=["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"], default="hybrid")
    ensure_supervisor.add_argument("--train-days", type=int, default=10)
    ensure_supervisor.add_argument("--test-days", type=int, default=3)
    ensure_supervisor.add_argument("--step-days", type=int, default=None)
    ensure_supervisor.add_argument("--top", type=int, default=3)
    ensure_supervisor.add_argument("--capture-poll-seconds", type=int, default=60)
    ensure_supervisor.add_argument("--supervisor-poll-seconds", type=int, default=300)
    ensure_supervisor.add_argument("--max-consecutive-errors", type=int, default=5)
    ensure_supervisor.add_argument("--paper-forward-available-eur", type=float, default=None)
    ensure_supervisor.add_argument("--paper-forward-duration-seconds", type=int, default=0)
    ensure_supervisor.add_argument("--enable-research-scans", action="store_true")
    ensure_supervisor.add_argument("--research-scan-available-eur", type=float, default=None)
    ensure_supervisor.add_argument("--research-scan-duration-seconds", type=int, default=90)
    ensure_supervisor.add_argument("--research-scan-max-messages", type=int, default=0)
    ensure_supervisor.add_argument("--research-scan-min-interval-seconds", type=int, default=900)
    ensure_supervisor.add_argument("--state-path", required=True)
    ensure_supervisor.add_argument("--supervisor-stdout-path", default=None)
    ensure_supervisor.add_argument("--supervisor-stderr-path", default=None)
    ensure_supervisor.add_argument("--paper-forward-stdout-path", default=None)
    ensure_supervisor.add_argument("--paper-forward-stderr-path", default=None)
    ensure_supervisor.add_argument("--skip-e2e-unit", action="store_true")
    ensure_supervisor.add_argument("--ignore-stop-request", action="store_true")

    supervisor_watchdog = sub.add_parser("supervisor-watchdog", help="Long-running watchdog that keeps the supervisor alive by restarting it when needed")
    supervisor_watchdog.add_argument("--data-dir", required=True)
    supervisor_watchdog.add_argument("--telemetry-path", default=None)
    supervisor_watchdog.add_argument("--setup", choices=["breakout", "recovery", "both"], default="both")
    supervisor_watchdog.add_argument("--profile", choices=["fast", "full"], default="fast")
    supervisor_watchdog.add_argument("--objective", choices=["hybrid", "profit_factor", "expectancy_eur", "expectancy_r"], default="hybrid")
    supervisor_watchdog.add_argument("--train-days", type=int, default=10)
    supervisor_watchdog.add_argument("--test-days", type=int, default=3)
    supervisor_watchdog.add_argument("--step-days", type=int, default=None)
    supervisor_watchdog.add_argument("--top", type=int, default=3)
    supervisor_watchdog.add_argument("--capture-poll-seconds", type=int, default=60)
    supervisor_watchdog.add_argument("--supervisor-poll-seconds", type=int, default=300)
    supervisor_watchdog.add_argument("--max-consecutive-errors", type=int, default=5)
    supervisor_watchdog.add_argument("--paper-forward-available-eur", type=float, default=None)
    supervisor_watchdog.add_argument("--paper-forward-duration-seconds", type=int, default=0)
    supervisor_watchdog.add_argument("--enable-research-scans", action="store_true")
    supervisor_watchdog.add_argument("--research-scan-available-eur", type=float, default=None)
    supervisor_watchdog.add_argument("--research-scan-duration-seconds", type=int, default=90)
    supervisor_watchdog.add_argument("--research-scan-max-messages", type=int, default=0)
    supervisor_watchdog.add_argument("--research-scan-min-interval-seconds", type=int, default=900)
    supervisor_watchdog.add_argument("--state-path", required=True)
    supervisor_watchdog.add_argument("--supervisor-stdout-path", default=None)
    supervisor_watchdog.add_argument("--supervisor-stderr-path", default=None)
    supervisor_watchdog.add_argument("--paper-forward-stdout-path", default=None)
    supervisor_watchdog.add_argument("--paper-forward-stderr-path", default=None)
    supervisor_watchdog.add_argument("--watchdog-poll-seconds", type=int, default=60)
    supervisor_watchdog.add_argument("--max-cycles", type=int, default=0)
    supervisor_watchdog.add_argument("--stop-path", default=None)
    supervisor_watchdog.add_argument("--skip-e2e-unit", action="store_true")

    monitor_supervisor = sub.add_parser("monitor-supervisor", help="Read the supervisor state file and show live runtime status, progress, and ETA")
    monitor_supervisor.add_argument("--state-path", required=True)

    render_supervisor_dashboard = sub.add_parser("render-supervisor-dashboard", help="Render a read-only HTML dashboard from the current supervisor state")
    render_supervisor_dashboard.add_argument("--state-path", required=True)
    render_supervisor_dashboard.add_argument("--output-path", default=None)
    render_supervisor_dashboard.add_argument("--refresh-seconds", type=int, default=60)

    serve_dashboard = sub.add_parser("serve-dashboard-app", help="Serve the read-only monitoring dashboard as a local web app")
    serve_dashboard.add_argument("--data-dir", default="data")
    serve_dashboard.add_argument("--logs-dir", default="logs/ops")
    serve_dashboard.add_argument("--state-path", default=None)
    serve_dashboard.add_argument("--host", default="127.0.0.1")
    serve_dashboard.add_argument("--port", type=int, default=8787)
    serve_dashboard.add_argument("--task-name", default="FlowBotSupervisorWatchdog")
    serve_dashboard.add_argument("--open-browser", action="store_true")

    device_runtime = sub.add_parser("device-runtime", help="Show the resolved per-device runtime layout")
    device_runtime.add_argument("--project-root", default=".")
    device_runtime.add_argument("--device-id", default=None)

    migrate_runtime = sub.add_parser("migrate-runtime-layout", help="Copy or move legacy data/logs into the per-device runtime layout")
    migrate_runtime.add_argument("--project-root", default=".")
    migrate_runtime.add_argument("--device-id", default=None)
    migrate_runtime.add_argument("--move", action="store_true")

    export_device = sub.add_parser("export-device-report", help="Write a Git-safe per-device runtime summary under reports/devices/<device-id>")
    export_device.add_argument("--project-root", default=".")
    export_device.add_argument("--device-id", default=None)

    bootstrap_device = sub.add_parser("bootstrap-device", help="Prepare a device runtime and create desktop launchers for watchdog, dashboard, and device-report export")
    bootstrap_device.add_argument("--project-root", default=".")
    bootstrap_device.add_argument("--device-id", default=None)
    bootstrap_device.add_argument("--desktop-dir", default=None)
    bootstrap_device.add_argument("--migrate-legacy", action="store_true")
    bootstrap_device.add_argument("--move-legacy", action="store_true")

    init_personal_journal = sub.add_parser("init-personal-journal", help="Create the local personal trading journal file if it does not exist")
    init_personal_journal.add_argument("--path", default=None)

    personal_journal_report = sub.add_parser("personal-journal-report", help="Summarize manually logged personal trades")
    personal_journal_report.add_argument("--path", default=None)

    append_personal = sub.add_parser("append-personal-trade", help="Append one personal/manual trade to the local journal")
    append_personal.add_argument("--path", default=None)
    append_personal.add_argument("--market", required=True)
    append_personal.add_argument("--instrument", required=True)
    append_personal.add_argument("--venue", default="")
    append_personal.add_argument("--side", default="long")
    append_personal.add_argument("--strategy-name", required=True)
    append_personal.add_argument("--setup-family", default="")
    append_personal.add_argument("--timeframe", required=True)
    append_personal.add_argument("--status", default="closed")
    append_personal.add_argument("--entry-ts", default=None)
    append_personal.add_argument("--exit-ts", default=None)
    append_personal.add_argument("--entry-price", type=float, default=None)
    append_personal.add_argument("--exit-price", type=float, default=None)
    append_personal.add_argument("--pnl-eur", type=float, required=True)
    append_personal.add_argument("--pnl-pct", type=float, default=None)
    append_personal.add_argument("--fees-eur", type=float, default=0.0)
    append_personal.add_argument("--size-notional-eur", type=float, default=None)
    append_personal.add_argument("--confidence-before", type=int, default=None)
    append_personal.add_argument("--confidence-after", type=int, default=None)
    append_personal.add_argument("--lesson", default="")
    append_personal.add_argument("--notes", default="")
    append_personal.add_argument("--tags", default="")
    append_personal.add_argument("--mistakes", default="")

    stop_runtime = sub.add_parser("stop-runtime", help="Request a clean stop for the supervisor and/or paper-forward runtime")
    stop_runtime.add_argument("--state-path", required=True)
    stop_runtime.add_argument("--scope", choices=["all", "supervisor", "paper-forward"], default="all")
    stop_runtime.add_argument("--grace-seconds", type=int, default=10)
    stop_runtime.add_argument("--force", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    bot_config, execution_config = load_config_from_env(project_root=Path.cwd())
    client = ThreeCommasSignalClient(bot_config, execution_config)

    if args.command == "sample-entry":
        intent = DayTradeIntent(
            pair=args.pair,
            entry_zone=args.price,
            stop_price=args.stop,
            trail_activation_r=bot_config.trail_activation_r,
            max_hold_min=bot_config.max_hold_minutes,
            budget_eur=args.budget,
            reason_code="manual_sample",
            score=80.0,
            quality="A",
        )
        _emit_json(client.build_entry_payload(intent))
        return

    if args.command == "sample-exit":
        trade = ActiveTrade(
            pair=args.pair,
            entry_ts=datetime.now(timezone.utc),
            entry_price=args.entry,
            initial_stop_price=args.entry * 0.99,
            stop_price=args.entry * 0.99,
            budget_eur=args.budget,
            reason_code="manual_sample",
            max_hold_min=bot_config.max_hold_minutes,
            trail_activation_r=bot_config.trail_activation_r,
        )
        _emit_json(client.build_exit_payload(trade, args.price))
        return

    if args.command == "backtest":
        report = CsvBacktester(bot_config, execution_config.with_mode("paper")).run(Path(args.data_dir))
        _emit_json(asdict(report))
        return

    if args.command == "download-ohlc":
        kraken_client = KrakenPublicClient()
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        results = {}
        for pair in bot_config.pairs:
            output_path = history_csv_path(data_dir, pair.symbol, args.interval)
            last = kraken_client.write_ohlc_csv(pair.symbol, interval=args.interval, output_path=output_path, since=args.since)
            results[pair.symbol] = {"output": str(output_path), "last": last}
        _emit_json(results)
        return

    if args.command == "sync-ohlc":
        kraken_client = KrakenPublicClient()
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        results = {}
        for pair in bot_config.pairs:
            output_path = history_csv_path(data_dir, pair.symbol, args.interval)
            results[pair.symbol] = kraken_client.sync_ohlc_csv(pair.symbol, interval=args.interval, output_path=output_path)
        _emit_json(results)
        return

    if args.command == "calibrate":
        report = run_calibration(
            Path(args.data_dir),
            bot_config,
            execution_config,
            top_n=args.top,
            profile=args.profile,
            setup_scope=args.setup,
            objective=args.objective,
        )
        _emit_json(asdict(report))
        return

    if args.command == "walk-forward":
        histories = load_local_histories(Path(args.data_dir), [pair.symbol for pair in bot_config.pairs])
        report = run_walk_forward(
            histories,
            bot_config,
            execution_config,
            setup_scope=args.setup,
            profile=args.profile,
            objective=args.objective,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            top_n=args.top,
        )
        _emit_json(asdict(report))
        return

    if args.command == "walk-forward-optimize":
        histories = load_local_histories(Path(args.data_dir), [pair.symbol for pair in bot_config.pairs])
        report = run_walk_forward_optimization(
            histories,
            bot_config,
            execution_config,
            setup_scope=args.setup,
            profile=args.profile,
            objective=args.objective,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            top_n=args.top,
        )
        _emit_json(asdict(report))
        return

    if args.command == "diagnose-signals":
        report = run_signal_diagnostics(Path(args.data_dir), bot_config)
        _emit_json(asdict(report))
        return

    if args.command == "debug-signals":
        report = run_signal_debug_report(Path(args.data_dir), bot_config)
        _emit_json(asdict(report))
        return

    if args.command == "sync-ohlc-loop":
        kraken_client = KrakenPublicClient()
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        history = []
        for cycle in range(args.cycles):
            cycle_result = {"cycle": cycle + 1, "pairs": {}}
            for pair in bot_config.pairs:
                output_path = history_csv_path(data_dir, pair.symbol, args.interval)
                cycle_result["pairs"][pair.symbol] = kraken_client.sync_ohlc_csv(
                    pair.symbol,
                    interval=args.interval,
                    output_path=output_path,
                )
            history.append(cycle_result)
            if cycle + 1 < args.cycles:
                sleep(args.sleep_seconds)
        _emit_json(history)
        return

    if args.command == "sync-history":
        history = run_sync_history(
            Path(args.data_dir),
            bot_config,
            cycles=args.cycles,
            sleep_seconds=args.sleep_seconds,
        )
        _emit_json(history)
        return

    if args.command == "capture-until-ready":
        report = run_sync_history_until_ready(
            Path(args.data_dir),
            bot_config,
            train_days=args.train_days,
            test_days=args.test_days,
            poll_seconds=args.poll_seconds,
            max_cycles=(None if args.max_cycles <= 0 else args.max_cycles),
            max_consecutive_errors=args.max_consecutive_errors,
        )
        _emit_json(asdict(report))
        return

    if args.command == "history-status":
        report = run_history_status(
            Path(args.data_dir),
            bot_config,
            train_days=args.train_days,
            test_days=args.test_days,
        )
        _emit_json(asdict(report))
        return

    if args.command == "live-scan":
        runtime_execution = execution_config.with_mode(args.mode)
        preflight = ThreeCommasSignalClient(bot_config, runtime_execution).live_preflight()
        if args.mode == "live" and not preflight["armed"]:
            _emit_json({"status": "blocked", "preflight": preflight})
            return
        report = run_live_scanner(
            bot_config,
            runtime_execution,
            available_eur=args.available_eur,
            duration_seconds=args.duration_seconds,
            max_messages=args.max_messages,
            bootstrap_dir=args.bootstrap_dir,
            stop_file=args.stop_file,
        )
        _emit_json({"preflight": preflight, "report": asdict(report)})
        return

    if args.command == "forward-report":
        telemetry_path = Path(args.telemetry_path) if args.telemetry_path else Path(bot_config.telemetry_path)
        report = run_forward_test_report(telemetry_path, bot_config)
        _emit_json(asdict(report))
        return

    if args.command == "paper-forward-gate":
        telemetry_path = Path(args.telemetry_path) if args.telemetry_path else Path(bot_config.telemetry_path)
        report = run_paper_forward_gate(
            Path(args.data_dir),
            bot_config,
            execution_config,
            telemetry_path=telemetry_path,
            setup_scope=args.setup,
            profile=args.profile,
            objective=args.objective,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            top_n=args.top,
            skip_e2e_unit=args.skip_e2e_unit,
        )
        _emit_json(asdict(report))
        return

    if args.command == "prepare-paper-forward":
        telemetry_path = Path(args.telemetry_path) if args.telemetry_path else Path(bot_config.telemetry_path)
        report = run_prepare_paper_forward(
            Path(args.data_dir),
            bot_config,
            execution_config,
            telemetry_path=telemetry_path,
            setup_scope=args.setup,
            profile=args.profile,
            objective=args.objective,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            top_n=args.top,
            poll_seconds=args.poll_seconds,
            max_cycles=(None if args.max_cycles <= 0 else args.max_cycles),
            max_consecutive_errors=args.max_consecutive_errors,
            skip_e2e_unit=args.skip_e2e_unit,
        )
        _emit_json(asdict(report))
        return

    if args.command == "paper-forward-supervisor":
        telemetry_path = Path(args.telemetry_path) if args.telemetry_path else Path(bot_config.telemetry_path)
        report = run_paper_forward_supervisor(
            Path(args.data_dir),
            bot_config,
            execution_config,
            telemetry_path=telemetry_path,
            setup_scope=args.setup,
            profile=args.profile,
            objective=args.objective,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            top_n=args.top,
            capture_poll_seconds=args.capture_poll_seconds,
            supervisor_poll_seconds=args.supervisor_poll_seconds,
            max_supervisor_cycles=(None if args.max_supervisor_cycles <= 0 else args.max_supervisor_cycles),
            max_consecutive_errors=args.max_consecutive_errors,
            skip_e2e_unit=args.skip_e2e_unit,
            paper_forward_available_eur=args.paper_forward_available_eur,
            paper_forward_duration_seconds=args.paper_forward_duration_seconds,
            enable_research_scans=args.enable_research_scans,
            research_scan_available_eur=args.research_scan_available_eur,
            research_scan_duration_seconds=args.research_scan_duration_seconds,
            research_scan_max_messages=(None if args.research_scan_max_messages <= 0 else args.research_scan_max_messages),
            research_scan_min_interval_seconds=args.research_scan_min_interval_seconds,
            state_path=(Path(args.state_path) if args.state_path else None),
            paper_forward_stdout_path=(Path(args.paper_forward_stdout_path) if args.paper_forward_stdout_path else None),
            paper_forward_stderr_path=(Path(args.paper_forward_stderr_path) if args.paper_forward_stderr_path else None),
        )
        _emit_json(asdict(report))
        return

    if args.command == "ensure-supervisor":
        telemetry_path = Path(args.telemetry_path) if args.telemetry_path else Path(bot_config.telemetry_path)
        report = run_ensure_supervisor(
            Path(args.data_dir),
            bot_config,
            execution_config,
            telemetry_path=telemetry_path,
            setup_scope=args.setup,
            profile=args.profile,
            objective=args.objective,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            top_n=args.top,
            capture_poll_seconds=args.capture_poll_seconds,
            supervisor_poll_seconds=args.supervisor_poll_seconds,
            max_consecutive_errors=args.max_consecutive_errors,
            skip_e2e_unit=args.skip_e2e_unit,
            paper_forward_available_eur=args.paper_forward_available_eur,
            paper_forward_duration_seconds=args.paper_forward_duration_seconds,
            enable_research_scans=args.enable_research_scans,
            research_scan_available_eur=args.research_scan_available_eur,
            research_scan_duration_seconds=args.research_scan_duration_seconds,
            research_scan_max_messages=(None if args.research_scan_max_messages <= 0 else args.research_scan_max_messages),
            research_scan_min_interval_seconds=args.research_scan_min_interval_seconds,
            state_path=Path(args.state_path),
            supervisor_stdout_path=(Path(args.supervisor_stdout_path) if args.supervisor_stdout_path else None),
            supervisor_stderr_path=(Path(args.supervisor_stderr_path) if args.supervisor_stderr_path else None),
            paper_forward_stdout_path=(Path(args.paper_forward_stdout_path) if args.paper_forward_stdout_path else None),
            paper_forward_stderr_path=(Path(args.paper_forward_stderr_path) if args.paper_forward_stderr_path else None),
            honor_stop_request=not args.ignore_stop_request,
        )
        _emit_json(asdict(report))
        return

    if args.command == "supervisor-watchdog":
        telemetry_path = Path(args.telemetry_path) if args.telemetry_path else Path(bot_config.telemetry_path)
        report = run_supervisor_watchdog(
            Path(args.data_dir),
            bot_config,
            execution_config,
            telemetry_path=telemetry_path,
            setup_scope=args.setup,
            profile=args.profile,
            objective=args.objective,
            train_days=args.train_days,
            test_days=args.test_days,
            step_days=args.step_days,
            top_n=args.top,
            capture_poll_seconds=args.capture_poll_seconds,
            supervisor_poll_seconds=args.supervisor_poll_seconds,
            max_consecutive_errors=args.max_consecutive_errors,
            skip_e2e_unit=args.skip_e2e_unit,
            paper_forward_available_eur=args.paper_forward_available_eur,
            paper_forward_duration_seconds=args.paper_forward_duration_seconds,
            enable_research_scans=args.enable_research_scans,
            research_scan_available_eur=args.research_scan_available_eur,
            research_scan_duration_seconds=args.research_scan_duration_seconds,
            research_scan_max_messages=(None if args.research_scan_max_messages <= 0 else args.research_scan_max_messages),
            research_scan_min_interval_seconds=args.research_scan_min_interval_seconds,
            state_path=Path(args.state_path),
            watchdog_poll_seconds=args.watchdog_poll_seconds,
            max_cycles=(None if args.max_cycles <= 0 else args.max_cycles),
            stop_path=(Path(args.stop_path) if args.stop_path else None),
            supervisor_stdout_path=(Path(args.supervisor_stdout_path) if args.supervisor_stdout_path else None),
            supervisor_stderr_path=(Path(args.supervisor_stderr_path) if args.supervisor_stderr_path else None),
            paper_forward_stdout_path=(Path(args.paper_forward_stdout_path) if args.paper_forward_stdout_path else None),
            paper_forward_stderr_path=(Path(args.paper_forward_stderr_path) if args.paper_forward_stderr_path else None),
        )
        _emit_json(asdict(report))
        return

    if args.command == "monitor-supervisor":
        report = run_monitor_supervisor(Path(args.state_path))
        _emit_json(asdict(report))
        return

    if args.command == "render-supervisor-dashboard":
        state_path = Path(args.state_path)
        payload = load_supervisor_state_payload(state_path)
        output_path = Path(args.output_path) if args.output_path else state_path.parent / "supervisor_dashboard.html"
        write_supervisor_dashboard(output_path, payload, refresh_seconds=args.refresh_seconds)
        _emit_json({"state_path": str(state_path), "output_path": str(output_path), "status": "ok"})
        return

    if args.command == "serve-dashboard-app":
        server, url = serve_dashboard_app(
            bot_config=bot_config,
            data_dir=Path(args.data_dir),
            logs_root=Path(args.logs_dir),
            state_path=(Path(args.state_path) if args.state_path else None),
            host=args.host,
            port=args.port,
            task_name=args.task_name,
            open_browser=args.open_browser,
        )
        print(json.dumps({"status": "serving", "url": url}, indent=2))
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return

    if args.command == "device-runtime":
        _emit_json(asdict(build_runtime_paths(project_root=args.project_root, device_id=args.device_id)))
        return

    if args.command == "migrate-runtime-layout":
        _emit_json(
            migrate_legacy_runtime(
                project_root=args.project_root,
                device_id=args.device_id,
                copy_only=not args.move,
            )
        )
        return

    if args.command == "export-device-report":
        _emit_json(export_device_report(project_root=args.project_root, device_id=args.device_id))
        return

    if args.command == "bootstrap-device":
        _emit_json(
            bootstrap_device_payload(
                project_root=args.project_root,
                device_id=args.device_id,
                desktop_dir=args.desktop_dir,
                migrate_legacy=args.migrate_legacy,
                move_legacy=args.move_legacy,
            )
        )
        return

    if args.command == "init-personal-journal":
        path = Path(args.path) if args.path else Path(bot_config.personal_journal_path)
        _emit_json({"path": str(ensure_personal_journal_path(path)), "status": "ready"})
        return

    if args.command == "personal-journal-report":
        path = Path(args.path) if args.path else Path(bot_config.personal_journal_path)
        _emit_json(asdict(run_personal_journal_report(path)))
        return

    if args.command == "append-personal-trade":
        path = Path(args.path) if args.path else Path(bot_config.personal_journal_path)
        entry = build_personal_trade_entry(
            market=args.market,
            instrument=args.instrument,
            venue=args.venue,
            side=args.side,
            strategy_name=args.strategy_name,
            setup_family=args.setup_family,
            timeframe=args.timeframe,
            status=args.status,
            entry_ts=args.entry_ts,
            exit_ts=args.exit_ts,
            entry_price=args.entry_price,
            exit_price=args.exit_price,
            pnl_eur=args.pnl_eur,
            pnl_pct=args.pnl_pct,
            fees_eur=args.fees_eur,
            size_notional_eur=args.size_notional_eur,
            confidence_before=args.confidence_before,
            confidence_after=args.confidence_after,
            lesson=args.lesson,
            notes=args.notes,
            tags=[item for item in args.tags.split(",") if item.strip()],
            mistakes=[item for item in args.mistakes.split(",") if item.strip()],
        )
        _emit_json(append_personal_trade(path, entry))
        return

    if args.command == "stop-runtime":
        report = run_stop_runtime(
            Path(args.state_path),
            scope=args.scope,
            grace_seconds=args.grace_seconds,
            force=args.force,
        )
        _emit_json(asdict(report))
        return


if __name__ == "__main__":
    main()
