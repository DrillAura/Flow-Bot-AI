from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any, Callable

from .config import BotConfig, ThreeCommasConfig
from .dashboard import write_supervisor_dashboard
from .fast_research_lab import build_fast_research_lab_payload
from .history import history_bounds, load_local_histories
from .kraken import KrakenPublicClient
from .live import LiveScanReport, run_live_scanner
from .personal_journal import build_personal_journal_payload, run_personal_journal_report
from .reporting import ForwardTestReport, run_forward_test_report
from .research import (
    CalibrationObjective,
    SetupScope,
    WalkForwardOptimizationReport,
    WalkForwardReport,
    run_walk_forward,
    run_walk_forward_optimization,
)
from .sessions import is_trade_window
from .strategy_lab import review_strategy_lab
from .storage import history_csv_path


@dataclass(frozen=True)
class PairHistoryStatus:
    symbol: str
    candles_1m: int
    candles_15m: int
    first_ts: str
    last_ts: str
    span_days: float


@dataclass(frozen=True)
class HistoryStatusReport:
    data_dir: str
    train_days: int
    test_days: int
    required_days: int
    available_days: float
    sufficient_history: bool
    pair_status: dict[str, PairHistoryStatus]


@dataclass(frozen=True)
class PaperForwardGateReport:
    e2e_ok: bool
    e2e_results: list[dict[str, Any]]
    history_status: HistoryStatusReport
    walk_forward_report: WalkForwardReport
    forward_report: ForwardTestReport
    ready_to_start_paper_forward: bool


@dataclass(frozen=True)
class CaptureCycleReport:
    cycle: int
    sync_result: list[dict[str, Any]]
    history_status: HistoryStatusReport
    error: str = ""


@dataclass(frozen=True)
class CaptureUntilReadyReport:
    data_dir: str
    train_days: int
    test_days: int
    poll_seconds: int
    max_cycles: int | None
    ready: bool
    stopped_reason: str
    cycles_run: int
    error_count: int
    initial_history_status: HistoryStatusReport
    final_history_status: HistoryStatusReport
    cycle_reports: list[CaptureCycleReport]


@dataclass(frozen=True)
class PreparePaperForwardReport:
    capture_report: CaptureUntilReadyReport
    walk_forward_optimization: WalkForwardOptimizationReport | None
    paper_forward_gate: PaperForwardGateReport | None
    ready_for_paper_forward: bool


@dataclass(frozen=True)
class SupervisorResearchScanReport:
    enabled: bool
    session_open: bool
    should_run: bool
    ran: bool
    status: str
    stopped_reason: str
    requested_duration_seconds: int
    requested_max_messages: int | None
    requested_available_eur: float
    started_at: str | None
    finished_at: str | None
    live_scan_report: dict[str, Any] | None


@dataclass(frozen=True)
class HistoryProgressEstimate:
    required_days: int
    available_days: float
    remaining_days: float
    progress_pct: float
    cycles_observed: int
    avg_growth_days_per_cycle: float | None
    avg_growth_days_per_hour: float | None
    estimated_cycles_to_ready: float | None
    estimated_seconds_to_ready: float | None
    estimated_ready_at: str | None


@dataclass(frozen=True)
class SupervisorDailySummary:
    date: str
    generated_at: str
    supervisor_status: str
    progress_pct: float | None
    available_days: float | None
    required_days: int | None
    eta: str | None
    last_errors: list[str]
    gate_status: str
    gate_ready: bool | None
    gate_blockers: list[str]
    paper_forward_status: str
    research_scan_status: str
    research_scan_last_run_at: str | None
    research_scan_last_error: str | None
    strategy_lab_status: str
    strategy_lab_champion: str | None
    strategy_lab_last_promotion_reason: str | None


@dataclass(frozen=True)
class PaperForwardLaunchReport:
    started: bool
    pid: int | None
    command: list[str]
    stdout_path: str
    stderr_path: str
    reason: str
    stop_path: str = ""


@dataclass(frozen=True)
class PaperForwardSupervisorReport:
    status: str
    stopped_reason: str
    supervisor_cycles: int
    updated_at: str
    supervisor_pid: int | None
    supervisor_stop_path: str
    paper_forward_pid: int | None
    paper_forward_stop_path: str
    history_progress: HistoryProgressEstimate | None
    daily_summary: SupervisorDailySummary | None
    daily_summary_json_path: str
    daily_summary_markdown_path: str
    dashboard_path: str
    state_path: str
    last_prepare_report: PreparePaperForwardReport | None
    launch_report: PaperForwardLaunchReport | None
    research_scan: SupervisorResearchScanReport | None
    strategy_lab: dict[str, Any] | None
    personal_journal: dict[str, Any] | None
    fast_research_lab: dict[str, Any] | None


@dataclass(frozen=True)
class RuntimeProcessStatus:
    name: str
    pid: int | None
    alive: bool
    stop_path: str
    stop_requested: bool


@dataclass(frozen=True)
class SupervisorMonitorReport:
    state_path: str
    state_exists: bool
    status: str
    stopped_reason: str
    updated_at: str | None
    state_age_seconds: float | None
    ready_for_paper_forward: bool | None
    history_progress: HistoryProgressEstimate | None
    daily_summary: SupervisorDailySummary | None
    daily_summary_json_path: str | None
    daily_summary_markdown_path: str | None
    dashboard_path: str | None
    research_scan: SupervisorResearchScanReport | None
    strategy_lab: dict[str, Any] | None
    personal_journal: dict[str, Any] | None
    fast_research_lab: dict[str, Any] | None
    supervisor: RuntimeProcessStatus | None
    paper_forward: RuntimeProcessStatus | None


@dataclass(frozen=True)
class StopRuntimeReport:
    state_path: str
    requested: bool
    scope: str
    grace_seconds: int
    force: bool
    supervisor: RuntimeProcessStatus | None
    paper_forward: RuntimeProcessStatus | None
    supervisor_force_terminated: bool
    paper_forward_force_terminated: bool


@dataclass(frozen=True)
class SupervisorEnsureReport:
    state_path: str
    launched: bool
    supervisor_running: bool
    reason: str
    pid: int | None
    command: list[str]
    stdout_path: str
    stderr_path: str


@dataclass(frozen=True)
class SupervisorWatchdogReport:
    cycles: int
    launched_count: int
    status: str
    stopped_reason: str
    updated_at: str
    state_path: str
    last_ensure: SupervisorEnsureReport | None


def run_history_status(data_dir: Path, bot_config: BotConfig, train_days: int, test_days: int) -> HistoryStatusReport:
    histories = load_local_histories(data_dir, [pair.symbol for pair in bot_config.pairs])
    if not histories:
        return HistoryStatusReport(
            data_dir=str(data_dir),
            train_days=train_days,
            test_days=test_days,
            required_days=train_days + test_days,
            available_days=0.0,
            sufficient_history=False,
            pair_status={},
        )

    pair_status: dict[str, PairHistoryStatus] = {}
    for symbol, history in histories.items():
        if not history.candles_1m or not history.candles_15m:
            pair_status[symbol] = PairHistoryStatus(
                symbol=symbol,
                candles_1m=len(history.candles_1m),
                candles_15m=len(history.candles_15m),
                first_ts="",
                last_ts="",
                span_days=0.0,
            )
            continue
        start, end = history.bounds()
        span_days = max((end - start).total_seconds() / 86_400.0, 0.0)
        pair_status[symbol] = PairHistoryStatus(
            symbol=symbol,
            candles_1m=len(history.candles_1m),
            candles_15m=len(history.candles_15m),
            first_ts=start.isoformat(),
            last_ts=end.isoformat(),
            span_days=span_days,
        )

    available_days = 0.0
    sufficient_history = False
    if all(history.candles_1m and history.candles_15m for history in histories.values()):
        start, end = history_bounds(histories)
        available_days = max((end - start).total_seconds() / 86_400.0, 0.0)
        sufficient_history = available_days >= (train_days + test_days)

    return HistoryStatusReport(
        data_dir=str(data_dir),
        train_days=train_days,
        test_days=test_days,
        required_days=train_days + test_days,
        available_days=available_days,
        sufficient_history=sufficient_history,
        pair_status=pair_status,
    )


def run_sync_history(
    data_dir: Path,
    bot_config: BotConfig,
    *,
    intervals: tuple[int, ...] = (1, 15),
    cycles: int = 1,
    sleep_seconds: int = 0,
) -> list[dict[str, Any]]:
    kraken_client = KrakenPublicClient()
    data_dir.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    for cycle in range(cycles):
        cycle_result: dict[str, Any] = {"cycle": cycle + 1, "intervals": {}}
        for interval in intervals:
            interval_result: dict[str, Any] = {}
            for pair in bot_config.pairs:
                output_path = history_csv_path(data_dir, pair.symbol, interval)
                interval_result[pair.symbol] = kraken_client.sync_ohlc_csv(pair.symbol, interval=interval, output_path=output_path)
            cycle_result["intervals"][f"{interval}m"] = interval_result
        history.append(cycle_result)
        if cycle + 1 < cycles and sleep_seconds > 0:
            sleep(sleep_seconds)
    return history


def run_paper_forward_gate(
    data_dir: Path,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    *,
    telemetry_path: Path,
    setup_scope: SetupScope = "both",
    profile: str = "fast",
    objective: CalibrationObjective = "hybrid",
    train_days: int = 10,
    test_days: int = 3,
    step_days: int | None = None,
    top_n: int = 3,
    skip_e2e_unit: bool = True,
    e2e_runner: Callable[[bool], dict[str, Any]] | None = None,
    walk_forward_runner: Callable[..., WalkForwardReport] | None = None,
) -> PaperForwardGateReport:
    if e2e_runner is None:
        e2e_payload = _run_e2e_harness(skip_unit=skip_e2e_unit)
    else:
        e2e_payload = e2e_runner(skip_e2e_unit)

    histories = load_local_histories(data_dir, [pair.symbol for pair in bot_config.pairs])
    history_status = run_history_status(data_dir, bot_config, train_days=train_days, test_days=test_days)
    if walk_forward_runner is None:
        walk_forward_report = run_walk_forward(
            histories,
            bot_config,
            execution_config,
            setup_scope=setup_scope,
            profile=profile,
            objective=objective,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            top_n=top_n,
        )
    else:
        walk_forward_report = walk_forward_runner(
            histories,
            bot_config,
            execution_config,
            setup_scope=setup_scope,
            profile=profile,
            objective=objective,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            top_n=top_n,
        )
    forward_report = run_forward_test_report(telemetry_path, bot_config)
    e2e_results = list(e2e_payload.get("results", []))
    e2e_ok = bool(e2e_results) and all(bool(result.get("ok")) for result in e2e_results)
    ready_to_start = e2e_ok and history_status.sufficient_history and not walk_forward_report.insufficient_history
    return PaperForwardGateReport(
        e2e_ok=e2e_ok,
        e2e_results=e2e_results,
        history_status=history_status,
        walk_forward_report=walk_forward_report,
        forward_report=forward_report,
        ready_to_start_paper_forward=ready_to_start,
    )


def run_sync_history_until_ready(
    data_dir: Path,
    bot_config: BotConfig,
    *,
    train_days: int = 10,
    test_days: int = 3,
    intervals: tuple[int, ...] = (1, 15),
    poll_seconds: int = 60,
    max_cycles: int | None = None,
    max_consecutive_errors: int = 5,
    sync_runner: Callable[..., list[dict[str, Any]]] | None = None,
    status_runner: Callable[[Path, BotConfig, int, int], HistoryStatusReport] | None = None,
    sleep_fn: Callable[[int], None] = sleep,
) -> CaptureUntilReadyReport:
    sync_runner = sync_runner or run_sync_history
    status_runner = status_runner or run_history_status
    initial_status = status_runner(data_dir, bot_config, train_days, test_days)
    if initial_status.sufficient_history:
        return CaptureUntilReadyReport(
            data_dir=str(data_dir),
            train_days=train_days,
            test_days=test_days,
            poll_seconds=poll_seconds,
            max_cycles=max_cycles,
            ready=True,
            stopped_reason="already_ready",
            cycles_run=0,
            error_count=0,
            initial_history_status=initial_status,
            final_history_status=initial_status,
            cycle_reports=[],
        )

    cycle_reports: list[CaptureCycleReport] = []
    final_status = initial_status
    stopped_reason = "max_cycles_reached"
    cycles_run = 0
    error_count = 0
    consecutive_errors = 0
    while max_cycles is None or cycles_run < max_cycles:
        cycles_run += 1
        try:
            sync_result = sync_runner(
                data_dir,
                bot_config,
                intervals=intervals,
                cycles=1,
                sleep_seconds=0,
            )
        except Exception as exc:
            error_count += 1
            consecutive_errors += 1
            cycle_reports.append(
                CaptureCycleReport(
                    cycle=cycles_run,
                    sync_result=[],
                    history_status=final_status,
                    error=str(exc),
                )
            )
            if consecutive_errors >= max_consecutive_errors:
                stopped_reason = "error_limit_reached"
                break
            if max_cycles is not None and cycles_run >= max_cycles:
                stopped_reason = "max_cycles_reached"
                break
            if poll_seconds > 0:
                sleep_fn(poll_seconds)
            continue

        consecutive_errors = 0
        final_status = status_runner(data_dir, bot_config, train_days, test_days)
        cycle_reports.append(
            CaptureCycleReport(
                cycle=cycles_run,
                sync_result=sync_result,
                history_status=final_status,
            )
        )
        if final_status.sufficient_history:
            stopped_reason = "ready"
            break
        if max_cycles is not None and cycles_run >= max_cycles:
            stopped_reason = "max_cycles_reached"
            break
        if poll_seconds > 0:
            sleep_fn(poll_seconds)

    return CaptureUntilReadyReport(
        data_dir=str(data_dir),
        train_days=train_days,
        test_days=test_days,
        poll_seconds=poll_seconds,
        max_cycles=max_cycles,
        ready=final_status.sufficient_history,
        stopped_reason=stopped_reason,
        cycles_run=cycles_run,
        error_count=error_count,
        initial_history_status=initial_status,
        final_history_status=final_status,
        cycle_reports=cycle_reports,
    )


def run_prepare_paper_forward(
    data_dir: Path,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    *,
    telemetry_path: Path,
    setup_scope: SetupScope = "both",
    profile: str = "fast",
    objective: CalibrationObjective = "hybrid",
    train_days: int = 10,
    test_days: int = 3,
    step_days: int | None = None,
    top_n: int = 3,
    poll_seconds: int = 60,
    max_cycles: int | None = None,
    max_consecutive_errors: int = 5,
    skip_e2e_unit: bool = True,
    capture_runner: Callable[..., CaptureUntilReadyReport] | None = None,
    optimization_runner: Callable[..., WalkForwardOptimizationReport] | None = None,
    gate_runner: Callable[..., PaperForwardGateReport] | None = None,
) -> PreparePaperForwardReport:
    capture_runner = capture_runner or run_sync_history_until_ready
    optimization_runner = optimization_runner or run_walk_forward_optimization
    gate_runner = gate_runner or run_paper_forward_gate

    capture_report = capture_runner(
        data_dir,
        bot_config,
        train_days=train_days,
        test_days=test_days,
        poll_seconds=poll_seconds,
        max_cycles=max_cycles,
        max_consecutive_errors=max_consecutive_errors,
    )
    if not capture_report.ready:
        return PreparePaperForwardReport(
            capture_report=capture_report,
            walk_forward_optimization=None,
            paper_forward_gate=None,
            ready_for_paper_forward=False,
        )

    histories = load_local_histories(data_dir, [pair.symbol for pair in bot_config.pairs])
    walk_forward_optimization = optimization_runner(
        histories,
        bot_config,
        execution_config,
        setup_scope=setup_scope,
        profile=profile,
        objective=objective,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        top_n=top_n,
    )
    paper_forward_gate = gate_runner(
        data_dir,
        bot_config,
        execution_config,
        telemetry_path=telemetry_path,
        setup_scope=setup_scope,
        profile=profile,
        objective=objective,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        top_n=top_n,
        skip_e2e_unit=skip_e2e_unit,
    )
    ready_for_paper_forward = (
        not walk_forward_optimization.insufficient_history
        and paper_forward_gate.ready_to_start_paper_forward
    )
    return PreparePaperForwardReport(
        capture_report=capture_report,
        walk_forward_optimization=walk_forward_optimization,
        paper_forward_gate=paper_forward_gate,
        ready_for_paper_forward=ready_for_paper_forward,
    )


def run_paper_forward_supervisor(
    data_dir: Path,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    *,
    telemetry_path: Path,
    setup_scope: SetupScope = "both",
    profile: str = "fast",
    objective: CalibrationObjective = "hybrid",
    train_days: int = 10,
    test_days: int = 3,
    step_days: int | None = None,
    top_n: int = 3,
    capture_poll_seconds: int = 60,
    supervisor_poll_seconds: int = 300,
    max_supervisor_cycles: int | None = None,
    max_consecutive_errors: int = 5,
    skip_e2e_unit: bool = True,
    paper_forward_available_eur: float | None = None,
    paper_forward_duration_seconds: int = 0,
    enable_research_scans: bool = False,
    research_scan_available_eur: float | None = None,
    research_scan_duration_seconds: int = 90,
    research_scan_max_messages: int | None = None,
    research_scan_min_interval_seconds: int = 900,
    paper_forward_stdout_path: Path | None = None,
    paper_forward_stderr_path: Path | None = None,
    state_path: Path | None = None,
    supervisor_stop_path: Path | None = None,
    paper_forward_stop_path: Path | None = None,
    prepare_runner: Callable[..., PreparePaperForwardReport] | None = None,
    launcher: Callable[..., PaperForwardLaunchReport] | None = None,
    research_scan_runner: Callable[..., LiveScanReport] | None = None,
    sleep_fn: Callable[[int], None] = sleep,
) -> PaperForwardSupervisorReport:
    prepare_runner = prepare_runner or run_prepare_paper_forward
    launcher = launcher or _launch_paper_forward_process
    state_path = state_path or (Path(bot_config.telemetry_path).parent / "paper_forward_supervisor_state.json")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_stop_path = supervisor_stop_path or (state_path.parent / "supervisor.stop")
    paper_forward_stop_path = paper_forward_stop_path or (state_path.parent / "paper_forward.stop")
    _clear_stop_file(supervisor_stop_path)
    _clear_stop_file(paper_forward_stop_path)

    cycles = 0
    last_prepare_report: PreparePaperForwardReport | None = None
    launch_report: PaperForwardLaunchReport | None = None
    research_scan: SupervisorResearchScanReport | None = None
    last_research_scan_at: datetime | None = None
    status = "waiting_for_history"
    stopped_reason = "max_supervisor_cycles_reached"
    progress_samples: list[tuple[datetime, float]] = []
    supervisor_pid = os.getpid()

    while max_supervisor_cycles is None or cycles < max_supervisor_cycles:
        if _stop_requested(supervisor_stop_path):
            status = "stopped"
            stopped_reason = "stop_requested"
            break
        cycles += 1
        cycle_now = datetime.now(timezone.utc)
        research_scan = _run_supervisor_research_scan(
            bot_config=bot_config,
            execution_config=execution_config,
            data_dir=data_dir,
            available_eur=research_scan_available_eur or paper_forward_available_eur or bot_config.initial_equity_eur,
            duration_seconds=research_scan_duration_seconds,
            max_messages=research_scan_max_messages,
            session_open=is_trade_window(cycle_now, bot_config),
            enabled=enable_research_scans,
            last_scan_at=last_research_scan_at,
            min_interval_seconds=research_scan_min_interval_seconds,
            paper_forward_running=bool(launch_report and launch_report.started and launch_report.pid and _pid_is_alive(launch_report.pid)),
            now=cycle_now,
            scan_runner=research_scan_runner,
        )
        if research_scan.ran:
            finished_at = research_scan.finished_at or research_scan.started_at
            last_research_scan_at = (
                datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
                if finished_at
                else cycle_now
            )
        last_prepare_report = prepare_runner(
            data_dir,
            bot_config,
            execution_config,
            telemetry_path=telemetry_path,
            setup_scope=setup_scope,
            profile=profile,
            objective=objective,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            top_n=top_n,
            poll_seconds=capture_poll_seconds,
            max_cycles=1,
            max_consecutive_errors=max_consecutive_errors,
            skip_e2e_unit=skip_e2e_unit,
        )
        progress_samples.append(
            (
                datetime.now(timezone.utc),
                last_prepare_report.capture_report.final_history_status.available_days,
            )
        )

        if not last_prepare_report.capture_report.ready:
            status = "waiting_for_history"
            stopped_reason = "awaiting_next_cycle"
            _write_supervisor_state(
                state_path,
                _build_supervisor_report(
                    bot_config=bot_config,
                    status=status,
                    stopped_reason=stopped_reason,
                    cycles=cycles,
                    state_path=state_path,
                    supervisor_pid=supervisor_pid,
                    supervisor_stop_path=supervisor_stop_path,
                    paper_forward_stop_path=paper_forward_stop_path,
                    last_prepare_report=last_prepare_report,
                    launch_report=launch_report,
                    progress_samples=progress_samples,
                    research_scan=research_scan,
                ),
            )
            if max_supervisor_cycles is not None and cycles >= max_supervisor_cycles:
                stopped_reason = "max_supervisor_cycles_reached"
                break
            if supervisor_poll_seconds > 0:
                sleep_fn(supervisor_poll_seconds)
            continue

        if not last_prepare_report.ready_for_paper_forward:
            status = "gate_failed"
            stopped_reason = "paper_forward_gate_failed"
            _write_supervisor_state(
                state_path,
                _build_supervisor_report(
                    bot_config=bot_config,
                    status=status,
                    stopped_reason=stopped_reason,
                    cycles=cycles,
                    state_path=state_path,
                    supervisor_pid=supervisor_pid,
                    supervisor_stop_path=supervisor_stop_path,
                    paper_forward_stop_path=paper_forward_stop_path,
                    last_prepare_report=last_prepare_report,
                    launch_report=launch_report,
                    progress_samples=progress_samples,
                    research_scan=research_scan,
                ),
            )
            break

        launch_report = launcher(
            data_dir=data_dir,
            bot_config=bot_config,
            execution_config=execution_config,
            available_eur=paper_forward_available_eur or bot_config.initial_equity_eur,
            duration_seconds=paper_forward_duration_seconds,
            stdout_path=paper_forward_stdout_path,
            stderr_path=paper_forward_stderr_path,
            stop_path=paper_forward_stop_path,
        )
        status = "paper_forward_started" if launch_report.started else "paper_forward_launch_failed"
        stopped_reason = "paper_forward_started" if launch_report.started else launch_report.reason
        _write_supervisor_state(
            state_path,
            _build_supervisor_report(
                bot_config=bot_config,
                status=status,
                stopped_reason=stopped_reason,
                cycles=cycles,
                state_path=state_path,
                supervisor_pid=supervisor_pid,
                supervisor_stop_path=supervisor_stop_path,
                paper_forward_stop_path=paper_forward_stop_path,
                last_prepare_report=last_prepare_report,
                launch_report=launch_report,
                progress_samples=progress_samples,
                research_scan=research_scan,
            ),
        )
        break

    report = _build_supervisor_report(
        bot_config=bot_config,
        status=status,
        stopped_reason=stopped_reason,
        cycles=cycles,
        state_path=state_path,
        supervisor_pid=supervisor_pid,
        supervisor_stop_path=supervisor_stop_path,
        paper_forward_stop_path=paper_forward_stop_path,
        last_prepare_report=last_prepare_report,
        launch_report=launch_report,
        progress_samples=progress_samples,
        research_scan=research_scan,
    )
    _write_supervisor_state(state_path, report)
    return report


def run_monitor_supervisor(state_path: Path) -> SupervisorMonitorReport:
    if not state_path.exists():
        return SupervisorMonitorReport(
            state_path=str(state_path),
            state_exists=False,
            status="missing",
            stopped_reason="state_file_missing",
            updated_at=None,
            state_age_seconds=None,
            ready_for_paper_forward=None,
            history_progress=None,
            daily_summary=None,
            daily_summary_json_path=None,
            daily_summary_markdown_path=None,
            dashboard_path=None,
            research_scan=None,
            strategy_lab=None,
            personal_journal=None,
            fast_research_lab=None,
            supervisor=None,
            paper_forward=None,
        )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    updated_at = payload.get("updated_at")
    state_age_seconds = None
    if updated_at:
        updated_dt = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        state_age_seconds = max((datetime.now(timezone.utc) - updated_dt).total_seconds(), 0.0)
    supervisor_stop_path, paper_forward_stop_path = _state_stop_paths(payload, state_path)
    supervisor = RuntimeProcessStatus(
        name="supervisor",
        pid=payload.get("supervisor_pid"),
        alive=_pid_is_alive(payload.get("supervisor_pid")),
        stop_path=str(supervisor_stop_path),
        stop_requested=_stop_requested(supervisor_stop_path),
    )
    paper_forward_pid = payload.get("paper_forward_pid") or ((payload.get("launch_report") or {}).get("pid"))
    paper_forward = RuntimeProcessStatus(
        name="paper_forward",
        pid=paper_forward_pid,
        alive=_pid_is_alive(paper_forward_pid),
        stop_path=str(paper_forward_stop_path),
        stop_requested=_stop_requested(paper_forward_stop_path),
    )
    history_progress = _deserialize_history_progress(payload.get("history_progress"))
    daily_summary = _deserialize_daily_summary(payload.get("daily_summary"))
    research_scan = _deserialize_research_scan(payload.get("research_scan"))
    strategy_lab = payload.get("strategy_lab")
    personal_journal = payload.get("personal_journal")
    fast_research_lab = payload.get("fast_research_lab")
    last_prepare = payload.get("last_prepare_report") or {}
    return SupervisorMonitorReport(
        state_path=str(state_path),
        state_exists=True,
        status=str(payload.get("status", "unknown")),
        stopped_reason=str(payload.get("stopped_reason", "")),
        updated_at=updated_at,
        state_age_seconds=state_age_seconds,
        ready_for_paper_forward=last_prepare.get("ready_for_paper_forward"),
        history_progress=history_progress,
        daily_summary=daily_summary,
        daily_summary_json_path=(str(payload["daily_summary_json_path"]) if payload.get("daily_summary_json_path") else None),
        daily_summary_markdown_path=(str(payload["daily_summary_markdown_path"]) if payload.get("daily_summary_markdown_path") else None),
        dashboard_path=(str(payload["dashboard_path"]) if payload.get("dashboard_path") else None),
        research_scan=research_scan,
        strategy_lab=strategy_lab if isinstance(strategy_lab, dict) else None,
        personal_journal=personal_journal if isinstance(personal_journal, dict) else None,
        fast_research_lab=fast_research_lab if isinstance(fast_research_lab, dict) else None,
        supervisor=supervisor,
        paper_forward=paper_forward,
    )


def run_stop_runtime(
    state_path: Path,
    *,
    scope: str = "all",
    grace_seconds: int = 10,
    force: bool = False,
    sleep_fn: Callable[[int], None] = sleep,
) -> StopRuntimeReport:
    monitor = run_monitor_supervisor(state_path)
    supervisor_force_terminated = False
    paper_forward_force_terminated = False
    if not monitor.state_exists:
        return StopRuntimeReport(
            state_path=str(state_path),
            requested=False,
            scope=scope,
            grace_seconds=grace_seconds,
            force=force,
            supervisor=monitor.supervisor,
            paper_forward=monitor.paper_forward,
            supervisor_force_terminated=False,
            paper_forward_force_terminated=False,
        )

    if scope in {"all", "supervisor"} and monitor.supervisor is not None:
        _write_stop_file(Path(monitor.supervisor.stop_path))
    if scope in {"all", "paper-forward"} and monitor.paper_forward is not None:
        _write_stop_file(Path(monitor.paper_forward.stop_path))

    if grace_seconds > 0:
        sleep_fn(grace_seconds)

    refreshed = run_monitor_supervisor(state_path)
    if force and scope in {"all", "supervisor"} and refreshed.supervisor and refreshed.supervisor.alive and refreshed.supervisor.pid:
        supervisor_force_terminated = _terminate_pid(refreshed.supervisor.pid)
    if force and scope in {"all", "paper-forward"} and refreshed.paper_forward and refreshed.paper_forward.alive and refreshed.paper_forward.pid:
        paper_forward_force_terminated = _terminate_pid(refreshed.paper_forward.pid)
    final_monitor = run_monitor_supervisor(state_path)
    return StopRuntimeReport(
        state_path=str(state_path),
        requested=True,
        scope=scope,
        grace_seconds=grace_seconds,
        force=force,
        supervisor=final_monitor.supervisor,
        paper_forward=final_monitor.paper_forward,
        supervisor_force_terminated=supervisor_force_terminated,
        paper_forward_force_terminated=paper_forward_force_terminated,
    )


def run_ensure_supervisor(
    data_dir: Path,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    *,
    telemetry_path: Path,
    setup_scope: SetupScope = "both",
    profile: str = "fast",
    objective: CalibrationObjective = "hybrid",
    train_days: int = 10,
    test_days: int = 3,
    step_days: int | None = None,
    top_n: int = 3,
    capture_poll_seconds: int = 60,
    supervisor_poll_seconds: int = 300,
    max_consecutive_errors: int = 5,
    skip_e2e_unit: bool = True,
    paper_forward_available_eur: float | None = None,
    paper_forward_duration_seconds: int = 0,
    enable_research_scans: bool = False,
    research_scan_available_eur: float | None = None,
    research_scan_duration_seconds: int = 90,
    research_scan_max_messages: int | None = None,
    research_scan_min_interval_seconds: int = 900,
    state_path: Path,
    supervisor_stdout_path: Path | None = None,
    supervisor_stderr_path: Path | None = None,
    paper_forward_stdout_path: Path | None = None,
    paper_forward_stderr_path: Path | None = None,
    honor_stop_request: bool = True,
    launcher: Callable[..., SupervisorEnsureReport] | None = None,
) -> SupervisorEnsureReport:
    launcher = launcher or _launch_supervisor_process
    monitor = run_monitor_supervisor(state_path)
    if monitor.state_exists and monitor.supervisor is not None:
        if monitor.supervisor.stop_requested and honor_stop_request:
            return SupervisorEnsureReport(
                state_path=str(state_path),
                launched=False,
                supervisor_running=False,
                reason="supervisor_stop_requested",
                pid=monitor.supervisor.pid,
                command=[],
                stdout_path=str(supervisor_stdout_path or (state_path.parent / "supervisor_stdout.log")),
                stderr_path=str(supervisor_stderr_path or (state_path.parent / "supervisor_stderr.log")),
            )
        if monitor.supervisor.alive:
            return SupervisorEnsureReport(
                state_path=str(state_path),
                launched=False,
                supervisor_running=True,
                reason="already_running",
                pid=monitor.supervisor.pid,
                command=[],
                stdout_path=str(supervisor_stdout_path or (state_path.parent / "supervisor_stdout.log")),
                stderr_path=str(supervisor_stderr_path or (state_path.parent / "supervisor_stderr.log")),
            )
    return launcher(
        data_dir=data_dir,
        bot_config=bot_config,
        execution_config=execution_config,
        telemetry_path=telemetry_path,
        setup_scope=setup_scope,
        profile=profile,
        objective=objective,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        top_n=top_n,
        capture_poll_seconds=capture_poll_seconds,
        supervisor_poll_seconds=supervisor_poll_seconds,
        max_consecutive_errors=max_consecutive_errors,
        skip_e2e_unit=skip_e2e_unit,
        paper_forward_available_eur=paper_forward_available_eur,
        paper_forward_duration_seconds=paper_forward_duration_seconds,
        enable_research_scans=enable_research_scans,
        research_scan_available_eur=research_scan_available_eur,
        research_scan_duration_seconds=research_scan_duration_seconds,
        research_scan_max_messages=research_scan_max_messages,
        research_scan_min_interval_seconds=research_scan_min_interval_seconds,
        state_path=state_path,
        supervisor_stdout_path=supervisor_stdout_path,
        supervisor_stderr_path=supervisor_stderr_path,
        paper_forward_stdout_path=paper_forward_stdout_path,
        paper_forward_stderr_path=paper_forward_stderr_path,
    )


def run_supervisor_watchdog(
    data_dir: Path,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    *,
    telemetry_path: Path,
    setup_scope: SetupScope = "both",
    profile: str = "fast",
    objective: CalibrationObjective = "hybrid",
    train_days: int = 10,
    test_days: int = 3,
    step_days: int | None = None,
    top_n: int = 3,
    capture_poll_seconds: int = 60,
    supervisor_poll_seconds: int = 300,
    max_consecutive_errors: int = 5,
    skip_e2e_unit: bool = True,
    paper_forward_available_eur: float | None = None,
    paper_forward_duration_seconds: int = 0,
    enable_research_scans: bool = False,
    research_scan_available_eur: float | None = None,
    research_scan_duration_seconds: int = 90,
    research_scan_max_messages: int | None = None,
    research_scan_min_interval_seconds: int = 900,
    state_path: Path,
    watchdog_poll_seconds: int = 60,
    max_cycles: int | None = None,
    stop_path: Path | None = None,
    supervisor_stdout_path: Path | None = None,
    supervisor_stderr_path: Path | None = None,
    paper_forward_stdout_path: Path | None = None,
    paper_forward_stderr_path: Path | None = None,
    sleep_fn: Callable[[int], None] = sleep,
    ensure_runner: Callable[..., SupervisorEnsureReport] | None = None,
) -> SupervisorWatchdogReport:
    ensure_runner = ensure_runner or run_ensure_supervisor
    stop_path = stop_path or (state_path.parent / "watchdog.stop")
    _clear_stop_file(stop_path)
    cycles = 0
    launched_count = 0
    last_ensure: SupervisorEnsureReport | None = None
    status = "watching"
    stopped_reason = "max_cycles_reached"

    while max_cycles is None or cycles < max_cycles:
        if _stop_requested(stop_path):
            status = "stopped"
            stopped_reason = "stop_requested"
            break
        cycles += 1
        last_ensure = ensure_runner(
            data_dir,
            bot_config,
            execution_config,
            telemetry_path=telemetry_path,
            setup_scope=setup_scope,
            profile=profile,
            objective=objective,
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            top_n=top_n,
            capture_poll_seconds=capture_poll_seconds,
            supervisor_poll_seconds=supervisor_poll_seconds,
            max_consecutive_errors=max_consecutive_errors,
            skip_e2e_unit=skip_e2e_unit,
            paper_forward_available_eur=paper_forward_available_eur,
            paper_forward_duration_seconds=paper_forward_duration_seconds,
            enable_research_scans=enable_research_scans,
            research_scan_available_eur=research_scan_available_eur,
            research_scan_duration_seconds=research_scan_duration_seconds,
            research_scan_max_messages=research_scan_max_messages,
            research_scan_min_interval_seconds=research_scan_min_interval_seconds,
            state_path=state_path,
            supervisor_stdout_path=supervisor_stdout_path,
            supervisor_stderr_path=supervisor_stderr_path,
            paper_forward_stdout_path=paper_forward_stdout_path,
            paper_forward_stderr_path=paper_forward_stderr_path,
        )
        if last_ensure.launched:
            launched_count += 1
        if max_cycles is not None and cycles >= max_cycles:
            stopped_reason = "max_cycles_reached"
            break
        if watchdog_poll_seconds > 0:
            sleep_fn(watchdog_poll_seconds)

    return SupervisorWatchdogReport(
        cycles=cycles,
        launched_count=launched_count,
        status=status,
        stopped_reason=stopped_reason,
        updated_at=datetime.now(timezone.utc).isoformat(),
        state_path=str(state_path),
        last_ensure=last_ensure,
    )


def _run_e2e_harness(skip_unit: bool) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[1]
    args = [sys.executable, str(root / "scripts" / "e2e_verify.py")]
    if skip_unit:
        args.append("--skip-unit")
    completed = subprocess.run(args, cwd=root, capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def _launch_paper_forward_process(
    *,
    data_dir: Path,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    available_eur: float,
    duration_seconds: int,
    stdout_path: Path | None,
    stderr_path: Path | None,
    stop_path: Path,
) -> PaperForwardLaunchReport:
    root = Path(__file__).resolve().parents[1]
    stdout_path = stdout_path or (Path(bot_config.telemetry_path).parent / "paper_forward_stdout.log")
    stderr_path = stderr_path or (Path(bot_config.telemetry_path).parent / "paper_forward_stderr.log")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "daytrading_bot.cli",
        "live-scan",
        "--available-eur",
        f"{available_eur}",
        "--duration-seconds",
        str(duration_seconds),
        "--bootstrap-dir",
        str(data_dir),
        "--mode",
        "paper",
        "--stop-file",
        str(stop_path),
    ]
    stdout_handle = stdout_path.open("a", encoding="utf-8")
    stderr_handle = stderr_path.open("a", encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    env = {**os.environ, "PYTHONUTF8": "1", "BOT_MODE": "paper"}
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        creationflags=creationflags,
        close_fds=True,
    )
    stdout_handle.close()
    stderr_handle.close()
    return PaperForwardLaunchReport(
        started=True,
        pid=process.pid,
        command=command,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        stop_path=str(stop_path),
        reason="started",
    )


def _launch_supervisor_process(
    *,
    data_dir: Path,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    telemetry_path: Path,
    setup_scope: SetupScope,
    profile: str,
    objective: CalibrationObjective,
    train_days: int,
    test_days: int,
    step_days: int | None,
    top_n: int,
    capture_poll_seconds: int,
    supervisor_poll_seconds: int,
    max_consecutive_errors: int,
    skip_e2e_unit: bool,
    paper_forward_available_eur: float | None,
    paper_forward_duration_seconds: int,
    enable_research_scans: bool,
    research_scan_available_eur: float | None,
    research_scan_duration_seconds: int,
    research_scan_max_messages: int | None,
    research_scan_min_interval_seconds: int,
    state_path: Path,
    supervisor_stdout_path: Path | None,
    supervisor_stderr_path: Path | None,
    paper_forward_stdout_path: Path | None,
    paper_forward_stderr_path: Path | None,
) -> SupervisorEnsureReport:
    root = Path(__file__).resolve().parents[1]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_stdout_path = supervisor_stdout_path or (state_path.parent / "supervisor_stdout.log")
    supervisor_stderr_path = supervisor_stderr_path or (state_path.parent / "supervisor_stderr.log")
    paper_forward_stdout_path = paper_forward_stdout_path or (state_path.parent / "paper_forward_stdout.log")
    paper_forward_stderr_path = paper_forward_stderr_path or (state_path.parent / "paper_forward_stderr.log")
    supervisor_stdout_path.parent.mkdir(parents=True, exist_ok=True)
    supervisor_stderr_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "daytrading_bot.cli",
        "paper-forward-supervisor",
        "--data-dir",
        str(data_dir),
        "--telemetry-path",
        str(telemetry_path),
        "--setup",
        str(setup_scope),
        "--profile",
        profile,
        "--objective",
        str(objective),
        "--train-days",
        str(train_days),
        "--test-days",
        str(test_days),
        "--top",
        str(top_n),
        "--capture-poll-seconds",
        str(capture_poll_seconds),
        "--supervisor-poll-seconds",
        str(supervisor_poll_seconds),
        "--max-consecutive-errors",
        str(max_consecutive_errors),
        "--state-path",
        str(state_path),
        "--paper-forward-stdout-path",
        str(paper_forward_stdout_path),
        "--paper-forward-stderr-path",
        str(paper_forward_stderr_path),
    ]
    if step_days is not None:
        command.extend(["--step-days", str(step_days)])
    if skip_e2e_unit:
        command.append("--skip-e2e-unit")
    if paper_forward_available_eur is not None:
        command.extend(["--paper-forward-available-eur", f"{paper_forward_available_eur}"])
    if paper_forward_duration_seconds is not None:
        command.extend(["--paper-forward-duration-seconds", str(paper_forward_duration_seconds)])
    if enable_research_scans:
        command.append("--enable-research-scans")
    if research_scan_available_eur is not None:
        command.extend(["--research-scan-available-eur", f"{research_scan_available_eur}"])
    if research_scan_duration_seconds is not None:
        command.extend(["--research-scan-duration-seconds", str(research_scan_duration_seconds)])
    if research_scan_max_messages is not None:
        command.extend(["--research-scan-max-messages", str(research_scan_max_messages)])
    if research_scan_min_interval_seconds is not None:
        command.extend(["--research-scan-min-interval-seconds", str(research_scan_min_interval_seconds)])
    stdout_handle = supervisor_stdout_path.open("a", encoding="utf-8")
    stderr_handle = supervisor_stderr_path.open("a", encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    env = {**os.environ, "PYTHONUTF8": "1", "BOT_MODE": execution_config.mode}
    process = subprocess.Popen(
        command,
        cwd=root,
        env=env,
        stdout=stdout_handle,
        stderr=stderr_handle,
        creationflags=creationflags,
        close_fds=True,
    )
    stdout_handle.close()
    stderr_handle.close()
    return SupervisorEnsureReport(
        state_path=str(state_path),
        launched=True,
        supervisor_running=True,
        reason="started",
        pid=process.pid,
        command=command,
        stdout_path=str(supervisor_stdout_path),
        stderr_path=str(supervisor_stderr_path),
    )


def _write_supervisor_state(path: Path, report: PaperForwardSupervisorReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(asdict(report), handle, indent=2, default=str)
    _write_daily_summary_artifacts(report)
    write_supervisor_dashboard(Path(report.dashboard_path), asdict(report), refresh_seconds=60)


def _build_supervisor_report(
    *,
    bot_config: BotConfig,
    status: str,
    stopped_reason: str,
    cycles: int,
    state_path: Path,
    supervisor_pid: int | None,
    supervisor_stop_path: Path,
    paper_forward_stop_path: Path,
    last_prepare_report: PreparePaperForwardReport | None,
    launch_report: PaperForwardLaunchReport | None,
    progress_samples: list[tuple[datetime, float]],
    research_scan: SupervisorResearchScanReport | None,
) -> PaperForwardSupervisorReport:
    required_days = 0
    if last_prepare_report is not None:
        required_days = last_prepare_report.capture_report.final_history_status.required_days
    history_progress = _build_history_progress(required_days, progress_samples) if required_days > 0 else None
    strategy_lab_review = review_strategy_lab(Path(bot_config.telemetry_path), bot_config)
    strategy_lab_payload = asdict(strategy_lab_review)
    personal_journal_payload = build_personal_journal_payload(run_personal_journal_report(Path(bot_config.personal_journal_path)))
    fast_research_lab_payload = build_fast_research_lab_payload(strategy_lab_payload, Path(bot_config.telemetry_path))
    paper_forward_pid = launch_report.pid if launch_report is not None else None
    summary_json_path, summary_markdown_path, dashboard_path = _artifact_paths(state_path, bot_config)
    daily_summary = _build_daily_summary(
        bot_config=bot_config,
        status=status,
        stopped_reason=stopped_reason,
        history_progress=history_progress,
        paper_forward_stop_path=paper_forward_stop_path,
        last_prepare_report=last_prepare_report,
        launch_report=launch_report,
        research_scan=research_scan,
        strategy_lab=strategy_lab_payload,
    )
    return PaperForwardSupervisorReport(
        status=status,
        stopped_reason=stopped_reason,
        supervisor_cycles=cycles,
        updated_at=datetime.now(timezone.utc).isoformat(),
        supervisor_pid=supervisor_pid,
        supervisor_stop_path=str(supervisor_stop_path),
        paper_forward_pid=paper_forward_pid,
        paper_forward_stop_path=str(paper_forward_stop_path),
        history_progress=history_progress,
        daily_summary=daily_summary,
        daily_summary_json_path=str(summary_json_path),
        daily_summary_markdown_path=str(summary_markdown_path),
        dashboard_path=str(dashboard_path),
        state_path=str(state_path),
        last_prepare_report=last_prepare_report,
        launch_report=launch_report,
        research_scan=research_scan,
        strategy_lab=strategy_lab_payload,
        personal_journal=personal_journal_payload,
        fast_research_lab=fast_research_lab_payload,
    )


def _build_history_progress(required_days: int, progress_samples: list[tuple[datetime, float]]) -> HistoryProgressEstimate:
    if not progress_samples:
        return HistoryProgressEstimate(
            required_days=required_days,
            available_days=0.0,
            remaining_days=float(required_days),
            progress_pct=0.0,
            cycles_observed=0,
            avg_growth_days_per_cycle=None,
            avg_growth_days_per_hour=None,
            estimated_cycles_to_ready=None,
            estimated_seconds_to_ready=None,
            estimated_ready_at=None,
        )

    start_ts, start_days = progress_samples[0]
    end_ts, end_days = progress_samples[-1]
    remaining_days = max(required_days - end_days, 0.0)
    progress_pct = min(max((end_days / required_days) * 100.0, 0.0), 100.0) if required_days > 0 else 0.0
    cycles_observed = len(progress_samples)
    avg_growth_days_per_cycle = None
    avg_growth_days_per_hour = None
    estimated_cycles_to_ready = None
    estimated_seconds_to_ready = None
    estimated_ready_at = None
    if cycles_observed > 1:
        growth_days = end_days - start_days
        elapsed_seconds = max((end_ts - start_ts).total_seconds(), 0.0)
        if growth_days > 0:
            avg_growth_days_per_cycle = growth_days / max(cycles_observed - 1, 1)
            if elapsed_seconds > 0:
                avg_growth_days_per_hour = growth_days / (elapsed_seconds / 3600.0)
            if avg_growth_days_per_cycle and avg_growth_days_per_cycle > 0:
                estimated_cycles_to_ready = remaining_days / avg_growth_days_per_cycle
            if avg_growth_days_per_hour and avg_growth_days_per_hour > 0:
                estimated_seconds_to_ready = (remaining_days / avg_growth_days_per_hour) * 3600.0
                estimated_ready_at = (end_ts + timedelta(seconds=estimated_seconds_to_ready)).isoformat()
    return HistoryProgressEstimate(
        required_days=required_days,
        available_days=end_days,
        remaining_days=remaining_days,
        progress_pct=progress_pct,
        cycles_observed=cycles_observed,
        avg_growth_days_per_cycle=avg_growth_days_per_cycle,
        avg_growth_days_per_hour=avg_growth_days_per_hour,
        estimated_cycles_to_ready=estimated_cycles_to_ready,
        estimated_seconds_to_ready=estimated_seconds_to_ready,
        estimated_ready_at=estimated_ready_at,
    )


def _artifact_paths(state_path: Path, bot_config: BotConfig) -> tuple[Path, Path, Path]:
    today = datetime.now(bot_config.timezone).date().isoformat()
    summary_json_path = state_path.parent / "supervisor_daily_summary.json"
    summary_markdown_path = state_path.parent / f"supervisor_daily_summary_{today}.md"
    dashboard_path = state_path.parent / "supervisor_dashboard.html"
    return summary_json_path, summary_markdown_path, dashboard_path


def _build_daily_summary(
    *,
    bot_config: BotConfig,
    status: str,
    stopped_reason: str,
    history_progress: HistoryProgressEstimate | None,
    paper_forward_stop_path: Path,
    last_prepare_report: PreparePaperForwardReport | None,
    launch_report: PaperForwardLaunchReport | None,
    research_scan: SupervisorResearchScanReport | None,
    strategy_lab: dict[str, Any] | None,
) -> SupervisorDailySummary:
    now_local = datetime.now(bot_config.timezone)
    gate_status, gate_ready, gate_blockers = _derive_gate_status(last_prepare_report)
    paper_forward_status = _derive_paper_forward_status(status, stopped_reason, paper_forward_stop_path, launch_report)
    research_scan_status, research_scan_last_run_at, research_scan_last_error = _derive_research_scan_status(research_scan)
    strategy_lab_status = "idle"
    strategy_lab_champion = None
    strategy_lab_last_promotion_reason = None
    if isinstance(strategy_lab, dict):
        strategy_lab_status = "active" if strategy_lab.get("strategies") else "idle"
        strategy_lab_champion = strategy_lab.get("current_paper_strategy_id")
        strategy_lab_last_promotion_reason = strategy_lab.get("promotion_reason")
    return SupervisorDailySummary(
        date=now_local.date().isoformat(),
        generated_at=now_local.isoformat(),
        supervisor_status=status,
        progress_pct=(history_progress.progress_pct if history_progress is not None else None),
        available_days=(history_progress.available_days if history_progress is not None else None),
        required_days=(history_progress.required_days if history_progress is not None else None),
        eta=(history_progress.estimated_ready_at if history_progress is not None else None),
        last_errors=_collect_summary_errors(last_prepare_report, launch_report, gate_blockers, research_scan),
        gate_status=gate_status,
        gate_ready=gate_ready,
        gate_blockers=gate_blockers,
        paper_forward_status=paper_forward_status,
        research_scan_status=research_scan_status,
        research_scan_last_run_at=research_scan_last_run_at,
        research_scan_last_error=research_scan_last_error,
        strategy_lab_status=strategy_lab_status,
        strategy_lab_champion=strategy_lab_champion,
        strategy_lab_last_promotion_reason=strategy_lab_last_promotion_reason,
    )


def _derive_research_scan_status(research_scan: SupervisorResearchScanReport | None) -> tuple[str, str | None, str | None]:
    if research_scan is None:
        return "disabled", None, None
    last_run_at = research_scan.finished_at or research_scan.started_at
    last_error = None
    if research_scan.status == "error":
        last_error = research_scan.stopped_reason or "research_scan_error"
    elif research_scan.status not in {"ok", "disabled", "skipped"} and research_scan.stopped_reason:
        last_error = research_scan.stopped_reason
    return research_scan.status, last_run_at, last_error


def _run_supervisor_research_scan(
    *,
    bot_config: BotConfig,
    execution_config: ThreeCommasConfig,
    data_dir: Path,
    available_eur: float,
    duration_seconds: int,
    max_messages: int | None,
    session_open: bool,
    enabled: bool,
    last_scan_at: datetime | None,
    min_interval_seconds: int,
    paper_forward_running: bool,
    now: datetime,
    scan_runner: Callable[..., LiveScanReport] | None = None,
) -> SupervisorResearchScanReport:
    scan_runner = scan_runner or run_live_scanner
    requested_max_messages = max_messages
    requested_available_eur = available_eur
    started_at = now.isoformat()
    if not enabled:
        return SupervisorResearchScanReport(
            enabled=False,
            session_open=session_open,
            should_run=False,
            ran=False,
            status="disabled",
            stopped_reason="research_scan_disabled",
            requested_duration_seconds=duration_seconds,
            requested_max_messages=requested_max_messages,
            requested_available_eur=requested_available_eur,
            started_at=None,
            finished_at=None,
            live_scan_report=None,
        )
    if not session_open:
        return SupervisorResearchScanReport(
            enabled=True,
            session_open=False,
            should_run=False,
            ran=False,
            status="skipped",
            stopped_reason="session_closed",
            requested_duration_seconds=duration_seconds,
            requested_max_messages=requested_max_messages,
            requested_available_eur=requested_available_eur,
            started_at=None,
            finished_at=None,
            live_scan_report=None,
        )
    if paper_forward_running:
        return SupervisorResearchScanReport(
            enabled=True,
            session_open=True,
            should_run=False,
            ran=False,
            status="skipped",
            stopped_reason="paper_forward_running",
            requested_duration_seconds=duration_seconds,
            requested_max_messages=requested_max_messages,
            requested_available_eur=requested_available_eur,
            started_at=None,
            finished_at=None,
            live_scan_report=None,
        )
    if last_scan_at is not None and min_interval_seconds > 0:
        elapsed = max((now - last_scan_at).total_seconds(), 0.0)
        if elapsed < min_interval_seconds:
            return SupervisorResearchScanReport(
                enabled=True,
                session_open=True,
                should_run=False,
                ran=False,
                status="skipped",
                stopped_reason="cooldown_active",
                requested_duration_seconds=duration_seconds,
                requested_max_messages=requested_max_messages,
                requested_available_eur=requested_available_eur,
                started_at=None,
                finished_at=None,
                live_scan_report=None,
            )
    try:
        report = scan_runner(
            bot_config,
            execution_config.with_mode("paper"),
            available_eur=available_eur,
            duration_seconds=duration_seconds,
            max_messages=max_messages,
            bootstrap_dir=str(data_dir),
            stop_file=None,
        )
    except Exception as exc:
        return SupervisorResearchScanReport(
            enabled=True,
            session_open=True,
            should_run=True,
            ran=True,
            status="error",
            stopped_reason=str(exc),
            requested_duration_seconds=duration_seconds,
            requested_max_messages=requested_max_messages,
            requested_available_eur=requested_available_eur,
            started_at=started_at,
            finished_at=now.isoformat(),
            live_scan_report=None,
        )
    return SupervisorResearchScanReport(
        enabled=True,
        session_open=True,
        should_run=True,
        ran=True,
        status=report.status,
        stopped_reason=report.error or "ok",
        requested_duration_seconds=duration_seconds,
        requested_max_messages=requested_max_messages,
        requested_available_eur=requested_available_eur,
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        live_scan_report=asdict(report),
    )


def _collect_summary_errors(
    last_prepare_report: PreparePaperForwardReport | None,
    launch_report: PaperForwardLaunchReport | None,
    gate_blockers: list[str],
    research_scan: SupervisorResearchScanReport | None = None,
    limit: int = 5,
) -> list[str]:
    errors: list[str] = []
    if research_scan is not None and research_scan.status == "error" and research_scan.stopped_reason:
        errors.append(f"research_scan: {research_scan.stopped_reason}")
    if last_prepare_report is not None:
        for cycle in reversed(last_prepare_report.capture_report.cycle_reports):
            if cycle.error:
                errors.append(f"capture_cycle_{cycle.cycle}: {cycle.error}")
        gate = last_prepare_report.paper_forward_gate
        if gate is not None and not gate.e2e_ok:
            failing_checks = [result.get("name", "unknown") for result in gate.e2e_results if not result.get("ok", False)]
            if failing_checks:
                errors.append(f"e2e: {', '.join(failing_checks)}")
        forward_report = gate.forward_report if gate is not None else None
        if forward_report is not None:
            failed_forward_gates = [name for name, gate_result in forward_report.gates.items() if not gate_result.passed]
            if failed_forward_gates:
                errors.append(f"forward_gates: {', '.join(failed_forward_gates)}")
    if launch_report is not None and not launch_report.started:
        errors.append(f"paper_forward_launch: {launch_report.reason}")
    for blocker in gate_blockers:
        if blocker not in errors:
            errors.append(blocker)
    deduped: list[str] = []
    for error in errors:
        if error not in deduped:
            deduped.append(error)
    return deduped[:limit]


def _derive_gate_status(last_prepare_report: PreparePaperForwardReport | None) -> tuple[str, bool | None, list[str]]:
    if last_prepare_report is None:
        return "pending", None, []
    if not last_prepare_report.capture_report.ready:
        return "waiting_for_history", False, ["local_oos_history_not_ready"]
    optimization = last_prepare_report.walk_forward_optimization
    if optimization is None or optimization.insufficient_history:
        return "waiting_for_walk_forward", False, ["walk_forward_insufficient_history"]
    gate = last_prepare_report.paper_forward_gate
    if gate is None:
        return "pending", None, []
    if gate.ready_to_start_paper_forward:
        return "green", True, []
    blockers: list[str] = []
    if not gate.e2e_ok:
        blockers.append("e2e_gate_failed")
    if not gate.history_status.sufficient_history:
        blockers.append("history_status_insufficient")
    if gate.walk_forward_report.insufficient_history:
        blockers.append("walk_forward_insufficient_history")
    for name, gate_result in gate.forward_report.gates.items():
        if not gate_result.passed:
            blockers.append(f"forward_gate:{name}")
    if not blockers:
        blockers.append("paper_forward_gate_failed")
    return "red", False, blockers


def _derive_paper_forward_status(
    supervisor_status: str,
    stopped_reason: str,
    paper_forward_stop_path: Path,
    launch_report: PaperForwardLaunchReport | None,
) -> str:
    if _stop_requested(paper_forward_stop_path):
        return "stop_requested"
    if launch_report is None:
        if supervisor_status == "waiting_for_history":
            return "idle"
        if supervisor_status == "gate_failed":
            return "blocked_by_gate"
        return "idle"
    if not launch_report.started:
        return "launch_failed"
    if launch_report.pid and _pid_is_alive(launch_report.pid):
        return "running"
    if supervisor_status == "paper_forward_started":
        return "started"
    return stopped_reason or "stopped"


def _write_daily_summary_artifacts(report: PaperForwardSupervisorReport) -> None:
    if report.daily_summary is None:
        return
    summary_payload = asdict(report.daily_summary)
    json_path = Path(report.daily_summary_json_path)
    markdown_path = Path(report.daily_summary_markdown_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary_payload, indent=2, default=str), encoding="utf-8")
    markdown_path.write_text(_render_daily_summary_markdown(report.daily_summary), encoding="utf-8")


def _render_daily_summary_markdown(summary: SupervisorDailySummary) -> str:
    progress_line = "n/a"
    if summary.progress_pct is not None and summary.available_days is not None and summary.required_days is not None:
        progress_line = f"{summary.progress_pct:.2f}% ({summary.available_days:.4f}/{summary.required_days} days)"
    eta_line = summary.eta or "n/a"
    errors = summary.last_errors or ["none"]
    blockers = summary.gate_blockers or ["none"]
    lines = [
        f"# Supervisor Daily Summary - {summary.date}",
        "",
        f"- Generated at: {summary.generated_at}",
        f"- Supervisor status: {summary.supervisor_status}",
        f"- Current progress: {progress_line}",
        f"- ETA: {eta_line}",
        f"- Gate status: {summary.gate_status}",
        f"- Gate ready: {summary.gate_ready}",
        f"- Paper-forward status: {summary.paper_forward_status}",
        f"- Research scan status: {summary.research_scan_status}",
        f"- Research last run: {summary.research_scan_last_run_at or 'n/a'}",
        f"- Research last error: {summary.research_scan_last_error or 'n/a'}",
        f"- Strategy lab status: {summary.strategy_lab_status}",
        f"- Strategy lab champion: {summary.strategy_lab_champion or 'n/a'}",
        f"- Strategy lab promotion reason: {summary.strategy_lab_last_promotion_reason or 'n/a'}",
        "- Last errors:",
    ]
    for error in errors:
        lines.append(f"  - {error}")
    lines.append("- Gate blockers:")
    for blocker in blockers:
        lines.append(f"  - {blocker}")
    lines.append("")
    return "\n".join(lines)


def _state_stop_paths(payload: dict[str, Any], state_path: Path) -> tuple[Path, Path]:
    supervisor_stop = Path(payload.get("supervisor_stop_path") or (state_path.parent / "supervisor.stop"))
    paper_forward_stop = Path(payload.get("paper_forward_stop_path") or (state_path.parent / "paper_forward.stop"))
    return supervisor_stop, paper_forward_stop


def _deserialize_history_progress(raw: Any) -> HistoryProgressEstimate | None:
    if not isinstance(raw, dict):
        return None
    return HistoryProgressEstimate(
        required_days=int(raw.get("required_days", 0)),
        available_days=float(raw.get("available_days", 0.0)),
        remaining_days=float(raw.get("remaining_days", 0.0)),
        progress_pct=float(raw.get("progress_pct", 0.0)),
        cycles_observed=int(raw.get("cycles_observed", 0)),
        avg_growth_days_per_cycle=(float(raw["avg_growth_days_per_cycle"]) if raw.get("avg_growth_days_per_cycle") is not None else None),
        avg_growth_days_per_hour=(float(raw["avg_growth_days_per_hour"]) if raw.get("avg_growth_days_per_hour") is not None else None),
        estimated_cycles_to_ready=(float(raw["estimated_cycles_to_ready"]) if raw.get("estimated_cycles_to_ready") is not None else None),
        estimated_seconds_to_ready=(float(raw["estimated_seconds_to_ready"]) if raw.get("estimated_seconds_to_ready") is not None else None),
        estimated_ready_at=(str(raw["estimated_ready_at"]) if raw.get("estimated_ready_at") else None),
    )


def _deserialize_daily_summary(raw: Any) -> SupervisorDailySummary | None:
    if not isinstance(raw, dict):
        return None
    return SupervisorDailySummary(
        date=str(raw.get("date", "")),
        generated_at=str(raw.get("generated_at", "")),
        supervisor_status=str(raw.get("supervisor_status", "")),
        progress_pct=(float(raw["progress_pct"]) if raw.get("progress_pct") is not None else None),
        available_days=(float(raw["available_days"]) if raw.get("available_days") is not None else None),
        required_days=(int(raw["required_days"]) if raw.get("required_days") is not None else None),
        eta=(str(raw["eta"]) if raw.get("eta") else None),
        last_errors=[str(value) for value in raw.get("last_errors", [])],
        gate_status=str(raw.get("gate_status", "")),
        gate_ready=(bool(raw["gate_ready"]) if raw.get("gate_ready") is not None else None),
        gate_blockers=[str(value) for value in raw.get("gate_blockers", [])],
        paper_forward_status=str(raw.get("paper_forward_status", "")),
        research_scan_status=str(raw.get("research_scan_status", "disabled")),
        research_scan_last_run_at=(str(raw["research_scan_last_run_at"]) if raw.get("research_scan_last_run_at") else None),
        research_scan_last_error=(str(raw["research_scan_last_error"]) if raw.get("research_scan_last_error") else None),
        strategy_lab_status=str(raw.get("strategy_lab_status", "idle")),
        strategy_lab_champion=(str(raw["strategy_lab_champion"]) if raw.get("strategy_lab_champion") else None),
        strategy_lab_last_promotion_reason=(
            str(raw["strategy_lab_last_promotion_reason"])
            if raw.get("strategy_lab_last_promotion_reason")
            else None
        ),
    )


def _deserialize_research_scan(raw: Any) -> SupervisorResearchScanReport | None:
    if not isinstance(raw, dict):
        return None
    return SupervisorResearchScanReport(
        enabled=bool(raw.get("enabled", False)),
        session_open=bool(raw.get("session_open", False)),
        should_run=bool(raw.get("should_run", False)),
        ran=bool(raw.get("ran", False)),
        status=str(raw.get("status", "disabled")),
        stopped_reason=str(raw.get("stopped_reason", "")),
        requested_duration_seconds=int(raw.get("requested_duration_seconds", 0)),
        requested_max_messages=(int(raw["requested_max_messages"]) if raw.get("requested_max_messages") is not None else None),
        requested_available_eur=float(raw.get("requested_available_eur", 0.0)),
        started_at=(str(raw["started_at"]) if raw.get("started_at") else None),
        finished_at=(str(raw["finished_at"]) if raw.get("finished_at") else None),
        live_scan_report=(dict(raw["live_scan_report"]) if isinstance(raw.get("live_scan_report"), dict) else None),
    )


def _stop_requested(path: Path) -> bool:
    return path.exists()


def _clear_stop_file(path: Path) -> None:
    if path.exists():
        path.unlink()


def _write_stop_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def _pid_is_alive(pid: Any) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and str(pid) in completed.stdout
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _terminate_pid(pid: int) -> bool:
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True
    except OSError:
        return False
