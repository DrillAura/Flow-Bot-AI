from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import BotConfig, ThreeCommasConfig
from .engine import BotEngine
from .history import LocalPairHistory, load_local_histories, slice_histories_by_timerange, strategy_warmup_cursor
from .kraken import KrakenPublicClient
from .telemetry import InMemoryTelemetry
from .sessions import is_trade_window


@dataclass(frozen=True)
class BacktestTradeLog:
    pair: str
    setup_type: str
    regime_label: str
    quality: str
    score: float
    entry_ts: str
    exit_ts: str
    hold_minutes: float
    entry_price: float
    exit_price: float
    initial_stop_price: float
    final_stop_price: float
    pnl_eur: float
    r_multiple: float
    exit_reason: str
    reason_code: str
    trailing_enabled: bool


@dataclass(frozen=True)
class ExitDistributionRow:
    exit_reason: str
    count: int
    share: float


@dataclass(frozen=True)
class SetupPerformance:
    setup_type: str
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    net_pnl_eur: float
    expectancy_eur: float
    expectancy_r: float
    average_hold_minutes: float
    exit_distribution: list[ExitDistributionRow]


@dataclass(frozen=True)
class BacktestTradeSummary:
    gross_profit_eur: float
    gross_loss_eur: float
    expectancy_eur: float
    expectancy_r: float
    average_hold_minutes: float
    exit_distribution: list[ExitDistributionRow]
    setup_performance: list[SetupPerformance]


@dataclass(frozen=True)
class BacktestReport:
    ending_equity: float
    total_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    days_tested: int
    trades_per_day: float
    gross_profit_eur: float
    gross_loss_eur: float
    expectancy_eur: float
    expectancy_r: float
    average_hold_minutes: float
    exit_distribution: list[ExitDistributionRow]
    setup_performance: list[SetupPerformance]
    trade_logs: list[BacktestTradeLog]


class CsvBacktester:
    def __init__(self, bot_config: BotConfig, execution_config: ThreeCommasConfig) -> None:
        self.bot_config = bot_config
        self.execution_config = execution_config

    def run(self, data_dir: Path) -> BacktestReport:
        histories = load_local_histories(data_dir, [pair.symbol for pair in self.bot_config.pairs])
        return self.run_histories(histories)

    def run_histories_window(
        self,
        histories: dict[str, LocalPairHistory],
        start: datetime | None = None,
        end: datetime | None = None,
        warmup: timedelta = timedelta(0),
    ) -> BacktestReport:
        return self.run_histories(histories, window_start=start, window_end=end, warmup=warmup)

    def run_histories(
        self,
        histories: dict[str, LocalPairHistory],
        window_start: datetime | None = None,
        window_end: datetime | None = None,
        warmup: timedelta = timedelta(0),
    ) -> BacktestReport:
        if window_start is not None or window_end is not None:
            histories = slice_histories_by_timerange(histories, start=window_start, end=window_end, warmup=warmup)
        if not histories or any(not history.candles_1m for history in histories.values()):
            return _empty_backtest_report(self.bot_config)
        telemetry = InMemoryTelemetry()
        engine = BotEngine(
            self.bot_config,
            ThreeCommasConfig(
                secret=self.execution_config.secret,
                bot_uuid=self.execution_config.bot_uuid,
                webhook_url=self.execution_config.webhook_url,
                mode="paper",
                allow_live=False,
            ),
            telemetry=telemetry,
        )
        index_limit = min(len(history.candles_1m) for history in histories.values())
        kraken = KrakenPublicClient()
        observed_days: set[date] = set()

        for cursor in range(strategy_warmup_cursor(), index_limit):
            moment = next(iter(histories.values())).candles_1m[cursor].ts
            if window_start is not None and moment < window_start:
                continue
            if window_end is not None and moment >= window_end:
                break
            engine.risk.roll_day(moment)
            if engine.risk.state.active_trade is None and not is_trade_window(moment, self.bot_config):
                continue

            contexts = []
            observed_days.add(moment.astimezone(self.bot_config.timezone).date())
            for symbol, history in histories.items():
                latest_close = history.candles_1m[cursor].close
                contexts.append(history.context_at(cursor, kraken.synthetic_order_book(symbol, latest_close)))
            if contexts:
                engine.process_market(contexts, available_eur=engine.risk.state.equity, moment=moment)

        state = engine.risk.state
        days_tested = max(len(observed_days), 1)
        trade_logs = build_backtest_trade_logs(telemetry.events)
        summary = summarize_trade_logs(trade_logs)
        return BacktestReport(
            ending_equity=state.equity,
            total_trades=state.total_trades,
            win_rate=state.win_rate,
            profit_factor=state.profit_factor,
            max_drawdown_pct=engine.risk.max_drawdown_pct,
            days_tested=days_tested,
            trades_per_day=state.total_trades / days_tested,
            gross_profit_eur=summary.gross_profit_eur,
            gross_loss_eur=summary.gross_loss_eur,
            expectancy_eur=summary.expectancy_eur,
            expectancy_r=summary.expectancy_r,
            average_hold_minutes=summary.average_hold_minutes,
            exit_distribution=summary.exit_distribution,
            setup_performance=summary.setup_performance,
            trade_logs=trade_logs,
        )


def build_backtest_trade_logs(events: list[dict[str, object]]) -> list[BacktestTradeLog]:
    trade_logs: list[BacktestTradeLog] = []
    for event in events:
        if event.get("event_type") not in {"exit_sent", "kill_switch_exit"}:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        trade_logs.append(
            BacktestTradeLog(
                pair=str(payload.get("pair", "")),
                setup_type=str(payload.get("setup_type", "")),
                regime_label=str(payload.get("regime_label", "")),
                quality=str(payload.get("quality", "")),
                score=float(payload.get("score", 0.0)),
                entry_ts=str(payload.get("entry_market_ts", "")),
                exit_ts=str(payload.get("market_ts", "")),
                hold_minutes=float(payload.get("hold_minutes", 0.0)),
                entry_price=float(payload.get("entry_price", 0.0)),
                exit_price=float(payload.get("price", 0.0)),
                initial_stop_price=float(payload.get("initial_stop_price", 0.0)),
                final_stop_price=float(payload.get("stop_price", 0.0)),
                pnl_eur=float(payload.get("pnl_eur", 0.0)),
                r_multiple=float(payload.get("r_multiple", 0.0)),
                exit_reason=str(payload.get("reason", "")),
                reason_code=str(payload.get("reason_code", "")),
                trailing_enabled=bool(payload.get("trailing_enabled", False)),
            )
        )
    return trade_logs


def summarize_trade_logs(trade_logs: list[BacktestTradeLog]) -> BacktestTradeSummary:
    total_trades = len(trade_logs)
    gross_profit = sum(log.pnl_eur for log in trade_logs if log.pnl_eur > 0.0)
    gross_loss = sum(abs(log.pnl_eur) for log in trade_logs if log.pnl_eur < 0.0)
    expectancy_eur = (sum(log.pnl_eur for log in trade_logs) / total_trades) if total_trades else 0.0
    expectancy_r = (sum(log.r_multiple for log in trade_logs) / total_trades) if total_trades else 0.0
    average_hold_minutes = (sum(log.hold_minutes for log in trade_logs) / total_trades) if total_trades else 0.0
    setup_groups: dict[str, list[BacktestTradeLog]] = defaultdict(list)
    for log in trade_logs:
        setup_groups[log.setup_type].append(log)
    return BacktestTradeSummary(
        gross_profit_eur=gross_profit,
        gross_loss_eur=gross_loss,
        expectancy_eur=expectancy_eur,
        expectancy_r=expectancy_r,
        average_hold_minutes=average_hold_minutes,
        exit_distribution=_build_exit_distribution(trade_logs),
        setup_performance=[
            _build_setup_performance(setup_type, logs)
            for setup_type, logs in sorted(setup_groups.items())
        ],
    )


def _build_setup_performance(setup_type: str, trade_logs: list[BacktestTradeLog]) -> SetupPerformance:
    total_trades = len(trade_logs)
    wins = sum(1 for log in trade_logs if log.pnl_eur > 0.0)
    losses = sum(1 for log in trade_logs if log.pnl_eur < 0.0)
    gross_profit = sum(log.pnl_eur for log in trade_logs if log.pnl_eur > 0.0)
    gross_loss = sum(abs(log.pnl_eur) for log in trade_logs if log.pnl_eur < 0.0)
    if gross_loss == 0.0:
        profit_factor = float("inf") if gross_profit > 0.0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss
    net_pnl = sum(log.pnl_eur for log in trade_logs)
    expectancy_eur = (net_pnl / total_trades) if total_trades else 0.0
    expectancy_r = (sum(log.r_multiple for log in trade_logs) / total_trades) if total_trades else 0.0
    average_hold_minutes = (sum(log.hold_minutes for log in trade_logs) / total_trades) if total_trades else 0.0
    return SetupPerformance(
        setup_type=setup_type,
        total_trades=total_trades,
        wins=wins,
        losses=losses,
        win_rate=(wins / total_trades) if total_trades else 0.0,
        profit_factor=profit_factor,
        net_pnl_eur=net_pnl,
        expectancy_eur=expectancy_eur,
        expectancy_r=expectancy_r,
        average_hold_minutes=average_hold_minutes,
        exit_distribution=_build_exit_distribution(trade_logs),
    )


def _build_exit_distribution(trade_logs: list[BacktestTradeLog]) -> list[ExitDistributionRow]:
    total_trades = len(trade_logs)
    counts = Counter(log.exit_reason for log in trade_logs)
    return [
        ExitDistributionRow(
            exit_reason=exit_reason,
            count=count,
            share=(count / total_trades) if total_trades else 0.0,
        )
        for exit_reason, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _empty_backtest_report(bot_config: BotConfig) -> BacktestReport:
    return BacktestReport(
        ending_equity=bot_config.initial_equity_eur,
        total_trades=0,
        win_rate=0.0,
        profit_factor=0.0,
        max_drawdown_pct=0.0,
        days_tested=0,
        trades_per_day=0.0,
        gross_profit_eur=0.0,
        gross_loss_eur=0.0,
        expectancy_eur=0.0,
        expectancy_r=0.0,
        average_hold_minutes=0.0,
        exit_distribution=[],
        setup_performance=[],
        trade_logs=[],
    )
