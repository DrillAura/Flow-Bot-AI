from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class JsonlTelemetry:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path else None
        self.events: list[dict[str, Any]] = []
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event_type: str, payload: dict[str, Any], event_ts: datetime | None = None) -> None:
        logged_ts = datetime.now(timezone.utc)
        line = {
            "ts": (event_ts or logged_ts).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "logged_ts": logged_ts.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "event_type": event_type,
            "payload": self._serialize(payload),
        }
        self.events.append(line)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, default=str) + "\n")

    def _serialize(self, payload: Any) -> Any:
        if is_dataclass(payload):
            return asdict(payload)
        if isinstance(payload, dict):
            return {key: self._serialize(value) for key, value in payload.items()}
        if isinstance(payload, (list, tuple)):
            return [self._serialize(value) for value in payload]
        return payload


class InMemoryTelemetry(JsonlTelemetry):
    def __init__(self) -> None:
        super().__init__(path=None)
