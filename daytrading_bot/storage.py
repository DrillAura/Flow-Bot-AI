from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from .indicators import aggregate_candles
from .models import Candle


def load_csv_candles(path: Path) -> list[Candle]:
    if not path.exists():
        return []
    candles: list[Candle] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        expected_fields = {"timestamp", "open", "high", "low", "close", "volume"}
        if not reader.fieldnames or not expected_fields.issubset(set(reader.fieldnames)):
            raise ValueError(f"Invalid OHLC CSV header in {path}")
        for row in reader:
            ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            candles.append(
                Candle(
                    ts=ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(row["volume"]),
                )
            )
    return candles


def write_csv_candles(path: Path, candles: list[Candle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "timestamp": candle.ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
            )
    tmp_path.replace(path)


def merge_candles(existing: list[Candle], incoming: list[Candle]) -> list[Candle]:
    by_ts: dict[datetime, Candle] = {candle.ts: candle for candle in existing}
    by_ts.update({candle.ts: candle for candle in incoming})
    return [by_ts[key] for key in sorted(by_ts.keys())]


def history_csv_path(data_dir: Path, symbol: str, interval: int) -> Path:
    if interval <= 0:
        raise ValueError("interval must be positive")
    if interval == 1:
        return data_dir / f"{symbol}.csv"
    return data_dir / f"{symbol}.{interval}m.csv"


def load_interval_candles(data_dir: Path, symbol: str, interval: int) -> list[Candle]:
    path = history_csv_path(data_dir, symbol, interval)
    if path.exists():
        return load_csv_candles(path)
    if interval == 1:
        return []
    base = load_csv_candles(history_csv_path(data_dir, symbol, 1))
    if not base:
        return []
    return aggregate_candles(base, interval)
