from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from daytrading_bot.config import BotConfig  # noqa: E402
from daytrading_bot.indicators import atr, last_value  # noqa: E402
from daytrading_bot.models import Candle, MarketContext, OrderBookSnapshot  # noqa: E402
from daytrading_bot.storage import history_csv_path, write_csv_candles  # noqa: E402
from tests.helpers import build_default_universe_contexts, build_recovery_context  # noqa: E402


@dataclass(frozen=True)
class StageResult:
    name: str
    ok: bool
    details: dict[str, Any]


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic end-to-end verification harness")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--skip-unit", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="e2e_verify_") as tempdir:
        temp_root = Path(tempdir)
        fixtures = build_fixtures(temp_root)
        results = run_harness(fixtures, skip_unit=args.skip_unit)
        payload = {
            "temp_root": str(temp_root),
            "results": [asdict(result) for result in results],
        }
        print(json.dumps(payload, indent=2))

        if args.keep_temp:
            print(json.dumps({"kept_temp_root": str(temp_root)}, indent=2))

        if not all(result.ok for result in results):
            raise SystemExit(1)


def run_harness(fixtures: dict[str, Any], skip_unit: bool = False) -> list[StageResult]:
    stages: list[StageResult] = []
    if not skip_unit:
        stages.append(_unit_stage())
    stages.append(_backtest_stage("positive_backtest", fixtures["positive_dir"], expect_trades=True))
    stages.append(_backtest_stage("negative_backtest", fixtures["negative_dir"], expect_trades=False))
    stages.append(_calibrate_stage(fixtures["positive_dir"]))
    stages.append(_debug_stage("positive_debug", fixtures["positive_dir"], expect_setups=True))
    stages.append(_debug_stage("negative_debug", fixtures["negative_dir"], expect_setups=False))
    stages.append(_forward_stage(fixtures["telemetry_path"]))
    stages.append(_live_scan_stage(fixtures["positive_dir"]))
    stages.append(_live_block_stage())
    return stages


def build_fixtures(root: Path) -> dict[str, Any]:
    positive_dir = root / "positive"
    negative_dir = root / "negative"
    positive_dir.mkdir(parents=True, exist_ok=True)
    negative_dir.mkdir(parents=True, exist_ok=True)

    positive_sources = build_default_universe_contexts(BotConfig())
    for symbol, context in positive_sources.items():
        candles_1m, candles_15m = _build_positive_series(context)
        write_csv_candles(history_csv_path(positive_dir, symbol, 1), candles_1m)
        write_csv_candles(history_csv_path(positive_dir, symbol, 15), candles_15m)

    for symbol in (pair.symbol for pair in BotConfig().pairs):
        context = build_no_trade_context(symbol)
        write_csv_candles(history_csv_path(negative_dir, symbol, 1), list(context.candles_1m))
        write_csv_candles(history_csv_path(negative_dir, symbol, 15), list(context.candles_15m))

    telemetry_path = root / "forward_report_sample.jsonl"
    telemetry_path.write_text(
        "\n".join(json.dumps(event) for event in build_forward_report_events()) + "\n",
        encoding="utf-8-sig",
    )

    return {
        "positive_dir": positive_dir,
        "negative_dir": negative_dir,
        "telemetry_path": telemetry_path,
    }


def build_no_trade_context(symbol: str) -> MarketContext:
    start = datetime(2026, 3, 23, 7, 0, tzinfo=timezone.utc)
    candles_15m: list[Candle] = []
    candles_5m: list[Candle] = []
    candles_1m: list[Candle] = []

    price_15 = 100.0
    for index in range(80):
        price_15 += 0.015 if index % 2 == 0 else -0.012
        candles_15m.append(_candle(start + timedelta(minutes=15 * index), price_15, 90.0, 0.20, 0.18))

    price_5 = 100.0
    for index in range(40):
        price_5 += 0.01 if index % 2 == 0 else -0.009
        candles_5m.append(_candle(start + timedelta(minutes=5 * index), price_5, 70.0, 0.12, 0.11))

    price_1 = 100.0
    for index in range(1300):
        price_1 += 0.003 if index % 2 == 0 else -0.0025
        candles_1m.append(_candle(start + timedelta(minutes=index), price_1, 35.0, 0.05, 0.05))

    order_book = OrderBookSnapshot(
        symbol=symbol,
        best_bid=candles_5m[-1].close - 0.01,
        best_ask=candles_5m[-1].close + 0.01,
        bid_volume_top5=500.0,
        ask_volume_top5=520.0,
    )
    atr_values = atr(candles_15m, 14)
    atr_current = last_value(atr_values) or 0.5
    atr_pct_current = 100.0 * atr_current / candles_15m[-1].close
    atr_history = [atr_pct_current * ratio for ratio in [0.8 + (i * 0.003) for i in range(80)]]

    return MarketContext(
        symbol=symbol,
        candles_1m=candles_1m,
        candles_5m=candles_5m,
        candles_15m=candles_15m,
        order_book=order_book,
        atr_pct_history_15m=atr_history,
    )


def _build_positive_series(context: MarketContext) -> tuple[list[Candle], list[Candle]]:
    # Align the synthetic trade window with the active session window so the
    # positive fixture can actually produce executions during backtests.
    prefix_15m = _make_bullish_prefix(70, start=datetime(2026, 3, 22, 13, 30, tzinfo=timezone.utc))
    prefix_1m = _expand_15m_to_1m(prefix_15m)
    tail_shift = prefix_15m[-1].ts + timedelta(minutes=15) - context.candles_15m[0].ts
    tail_15m = [_shift_candle(candle, tail_shift) for candle in context.candles_15m]
    tail_1m = _expand_5m_to_1m([_shift_candle(candle, tail_shift) for candle in context.candles_5m])
    return prefix_1m + tail_1m, prefix_15m + tail_15m


def build_forward_report_events() -> list[dict[str, Any]]:
    return [
        {
            "ts": "2026-03-23T08:25:00Z",
            "event_type": "entry_rejected",
            "payload": {"pair": "XBTEUR", "reason": "min_notional"},
        },
        {
            "ts": "2026-03-23T08:30:00Z",
            "event_type": "entry_sent",
            "payload": {"intent": {"pair": "XBTEUR"}, "response": {"ok": True, "dry_run": True}},
        },
        {
            "ts": "2026-03-23T09:00:00Z",
            "event_type": "exit_sent",
            "payload": {"pair": "XBTEUR", "reason": "time_stop", "pnl_eur": 4.0, "response": {"ok": True, "dry_run": True}},
        },
        {
            "ts": "2026-03-23T21:50:00Z",
            "event_type": "entry_sent",
            "payload": {"intent": {"pair": "ETHEUR"}, "response": {"ok": True, "dry_run": True}},
        },
        {
            "ts": "2026-03-24T06:10:00Z",
            "event_type": "exit_sent",
            "payload": {"pair": "ETHEUR", "reason": "session_flat", "pnl_eur": -2.0, "response": {"ok": True, "dry_run": True}},
        },
    ]


def _expand_5m_to_1m(candles_5m: list[Candle]) -> list[Candle]:
    candles_1m: list[Candle] = []
    for candle in candles_5m:
        closes = [
            candle.open + ((candle.close - candle.open) * fraction)
            for fraction in (0.2, 0.4, 0.6, 0.8, 1.0)
        ]
        open_price = candle.open
        for index, close_price in enumerate(closes):
            if index == 0:
                open_value = open_price
            else:
                open_value = candles_1m[-1].close
            high = max(open_value, close_price)
            low = min(open_value, close_price)
            if index == 2:
                high = max(high, candle.high)
                low = min(low, candle.low)
            candles_1m.append(
                Candle(
                    ts=candle.ts + timedelta(minutes=index),
                    open=open_value,
                    high=high,
                    low=low,
                    close=close_price,
                    volume=candle.volume / 5.0,
                )
            )
    return candles_1m


def _shift_candles(candles: list[Candle], shift: timedelta) -> list[Candle]:
    return [_candle(candle.ts + shift, candle.close, candle.volume, candle.high - candle.close, candle.close - candle.low) for candle in candles]


def _expand_15m_to_1m(candles_15m: list[Candle]) -> list[Candle]:
    candles_1m: list[Candle] = []
    for candle in candles_15m:
        closes = [
            candle.open + ((candle.close - candle.open) * fraction)
            for fraction in (1 / 15, 2 / 15, 3 / 15, 4 / 15, 5 / 15, 6 / 15, 7 / 15, 8 / 15, 9 / 15, 10 / 15, 11 / 15, 12 / 15, 13 / 15, 14 / 15, 1.0)
        ]
        for index, close_price in enumerate(closes):
            open_value = candle.open if index == 0 else candles_1m[-1].close
            high = max(open_value, close_price)
            low = min(open_value, close_price)
            if index == 7:
                high = max(high, candle.high)
                low = min(low, candle.low)
            candles_1m.append(
                Candle(
                    ts=candle.ts + timedelta(minutes=index),
                    open=open_value,
                    high=high,
                    low=low,
                    close=close_price,
                    volume=candle.volume / 15.0,
                )
            )
    return candles_1m


def _make_bullish_prefix(count: int, start: datetime) -> list[Candle]:
    candles: list[Candle] = []
    price = 80.0
    for index in range(count):
        price += 0.3
        ts = start + timedelta(minutes=15 * index)
        candles.append(Candle(ts=ts, open=price - 0.25, high=price + 0.45, low=price - 0.30, close=price, volume=120.0))
    return candles


def _shift_candle(candle: Candle, delta: timedelta) -> Candle:
    return Candle(
        ts=candle.ts + delta,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
    )


def _unit_stage() -> StageResult:
    completed = _run_command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    return StageResult("unit_tests", completed.returncode == 0, {"returncode": completed.returncode})


def _backtest_stage(name: str, data_dir: Path, expect_trades: bool) -> StageResult:
    report = _run_cli_json(["backtest", "--data-dir", str(data_dir)])
    total_trades = int(report["total_trades"])
    exit_total = _sum_exit_distribution(report.get("exit_distribution", []))
    setup_total = _sum_setup_totals(report.get("setup_performance", []))
    ok = exit_total == total_trades and setup_total == total_trades
    if expect_trades:
        ok = ok and total_trades > 0 and len(report.get("trade_logs", [])) == total_trades
    return StageResult(name, ok, {"total_trades": total_trades, "exit_total": exit_total, "setup_total": setup_total})


def _calibrate_stage(data_dir: Path) -> StageResult:
    report = _run_cli_json(["calibrate", "--data-dir", str(data_dir), "--top", "3", "--profile", "fast"])
    top_results = report.get("top_results", [])
    scores = [float(row["score"]) for row in top_results]
    ok = report.get("variants_tested") == 8 and scores == sorted(scores, reverse=True)
    ok = ok and all("expectancy_eur" in row and "expectancy_r" in row for row in top_results)
    return StageResult("calibration", ok, {"variants_tested": report.get("variants_tested"), "top_count": len(top_results)})


def _debug_stage(name: str, data_dir: Path, expect_setups: bool) -> StageResult:
    report = _run_cli_json(["debug-signals", "--data-dir", str(data_dir)])
    setups_found = int(report.get("setups_found", 0))
    ok = setups_found > 0 if expect_setups else setups_found == 0
    return StageResult(name, ok, {"setups_found": setups_found})


def _forward_stage(telemetry_path: Path) -> StageResult:
    report = _run_cli_json(["forward-report", "--telemetry-path", str(telemetry_path)])
    gates = report.get("gates", {})
    ok = report.get("closed_trades") == 2 and gates.get("profit_factor", {}).get("passed") is True
    return StageResult("forward_report", ok, {"closed_trades": report.get("closed_trades")})


def _live_scan_stage(bootstrap_dir: Path) -> StageResult:
    report = _run_cli_json(
        [
            "live-scan",
            "--available-eur",
            "100",
            "--duration-seconds",
            "5",
            "--max-messages",
            "20",
            "--bootstrap-dir",
            str(bootstrap_dir),
            "--mode",
            "paper",
        ]
    )
    ok = report.get("preflight", {}).get("armed") is True and report.get("report", {}).get("status") == "ok"
    ok = ok and report.get("report", {}).get("contexts_built", 0) > 0
    return StageResult("live_scan_paper", ok, {"status": report.get("report", {}).get("status")})


def _live_block_stage() -> StageResult:
    completed = _run_command(
        [
            sys.executable,
            "-m",
            "daytrading_bot.cli",
            "live-scan",
            "--mode",
            "live",
            "--duration-seconds",
            "1",
            "--max-messages",
            "1",
        ],
        env={
            **os.environ,
            "BOT_MODE": "live",
            "BOT_ALLOW_LIVE": "false",
            "THREE_COMMAS_SECRET": "",
            "THREE_COMMAS_BOT_UUID": "",
        },
    )
    ok = completed.returncode == 0 and "\"status\": \"blocked\"" in completed.stdout
    return StageResult("live_block", ok, {"returncode": completed.returncode})


def _run_cli_json(args: list[str]) -> dict[str, Any]:
    completed = _run_command([sys.executable, "-m", "daytrading_bot.cli", *args])
    return json.loads(completed.stdout)


def _run_command(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    runtime_env = {**os.environ, "PYTHONUTF8": "1"}
    if env is not None:
        runtime_env.update(env)
    completed = subprocess.run(
        args,
        cwd=ROOT,
        env=runtime_env,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Command failed: {args}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}")
    return completed


def _sum_exit_distribution(rows: list[dict[str, Any]]) -> int:
    return sum(int(row.get("count", 0)) for row in rows)


def _sum_setup_totals(rows: list[dict[str, Any]]) -> int:
    return sum(int(row.get("total_trades", 0)) for row in rows)


def _candle(ts: datetime, close: float, volume: float, high_offset: float, low_offset: float) -> Candle:
    return Candle(
        ts=ts,
        open=close - 0.10,
        high=close + high_offset,
        low=close - low_offset,
        close=close,
        volume=volume,
    )


if __name__ == "__main__":
    main()
