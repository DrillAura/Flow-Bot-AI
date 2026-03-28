from __future__ import annotations

from datetime import datetime, timedelta

from .config import BotConfig


def localize(moment: datetime, config: BotConfig) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=config.timezone)
    return moment.astimezone(config.timezone)


def is_trade_window(moment: datetime, config: BotConfig) -> bool:
    local_moment = localize(moment, config)
    local_time = local_moment.time()
    return any(window.start <= local_time <= window.end for window in config.trade_windows)


def session_label(moment: datetime, config: BotConfig) -> str:
    local_moment = localize(moment, config)
    local_time = local_moment.time()
    if config.trade_windows[0].start <= local_time <= config.trade_windows[0].end:
        return "morning"
    if len(config.trade_windows) > 1 and config.trade_windows[1].start <= local_time <= config.trade_windows[1].end:
        return "afternoon"
    return "off_hours"


def is_hard_flat_time(moment: datetime, config: BotConfig) -> bool:
    local_moment = localize(moment, config)
    return local_moment.time() >= config.hard_flat_time


def next_trade_day_start(moment: datetime, config: BotConfig) -> datetime:
    local_moment = localize(moment, config)
    next_day = local_moment.date()
    if local_moment.time() >= config.trade_windows[0].start:
        next_day = next_day + timedelta(days=1)
    return datetime.combine(next_day, config.trade_windows[0].start, tzinfo=config.timezone)
