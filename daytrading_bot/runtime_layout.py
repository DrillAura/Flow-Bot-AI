from __future__ import annotations

import os
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    project_root: str
    device_id: str
    runtime_root: str
    data_dir: str
    logs_dir: str
    ops_logs_dir: str
    telemetry_path: str
    strategy_lab_state_path: str


def sanitize_device_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", (value or "").strip()).strip("-_.").lower()
    return normalized or "default-device"


def resolve_project_root(project_root: str | Path | None = None) -> Path:
    if project_root is not None:
        return Path(project_root).expanduser().resolve()
    env_root = os.getenv("FLOW_BOT_PROJECT_ROOT", "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_device_id(device_id: str | None = None) -> str:
    raw = (
        device_id
        or os.getenv("FLOW_BOT_DEVICE_ID", "").strip()
        or os.getenv("BOT_DEVICE_ID", "").strip()
        or os.getenv("COMPUTERNAME", "").strip()
        or os.getenv("HOSTNAME", "").strip()
        or "default-device"
    )
    return sanitize_device_id(raw)


def resolve_runtime_root(project_root: str | Path | None = None, device_id: str | None = None) -> Path:
    explicit_root = os.getenv("FLOW_BOT_RUNTIME_ROOT", "").strip()
    if explicit_root:
        return Path(explicit_root).expanduser().resolve()
    root = resolve_project_root(project_root)
    return root / ".runtime" / resolve_device_id(device_id)


def build_runtime_paths(project_root: str | Path | None = None, device_id: str | None = None) -> RuntimePaths:
    root = resolve_project_root(project_root)
    resolved_device_id = resolve_device_id(device_id)
    runtime_root = resolve_runtime_root(root, resolved_device_id)
    data_dir = runtime_root / "data"
    logs_dir = runtime_root / "logs"
    ops_logs_dir = logs_dir / "ops"
    telemetry_path = logs_dir / "trading_events.jsonl"
    strategy_lab_state_path = logs_dir / "strategy_lab_state.json"
    return RuntimePaths(
        project_root=str(root),
        device_id=resolved_device_id,
        runtime_root=str(runtime_root),
        data_dir=str(data_dir),
        logs_dir=str(logs_dir),
        ops_logs_dir=str(ops_logs_dir),
        telemetry_path=str(telemetry_path),
        strategy_lab_state_path=str(strategy_lab_state_path),
    )


def ensure_runtime_dirs(paths: RuntimePaths) -> RuntimePaths:
    for raw_path in (paths.runtime_root, paths.data_dir, paths.logs_dir, paths.ops_logs_dir):
        Path(raw_path).mkdir(parents=True, exist_ok=True)
    return paths


def migrate_legacy_runtime(
    project_root: str | Path | None = None,
    device_id: str | None = None,
    *,
    copy_only: bool = True,
) -> dict[str, object]:
    paths = ensure_runtime_dirs(build_runtime_paths(project_root, device_id))
    root = Path(paths.project_root)
    operations: list[dict[str, str]] = []
    legacy_mappings = {
        root / "data": Path(paths.data_dir),
        root / "logs": Path(paths.logs_dir),
    }
    for source, destination in legacy_mappings.items():
        if not source.exists():
            operations.append({"source": str(source), "destination": str(destination), "status": "missing"})
            continue
        destination.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
            if not copy_only:
                shutil.rmtree(source)
        else:
            shutil.copy2(source, destination)
            if not copy_only:
                source.unlink(missing_ok=True)
        operations.append(
            {
                "source": str(source),
                "destination": str(destination),
                "status": "copied" if copy_only else "moved",
            }
        )
    payload = asdict(paths)
    payload["copy_only"] = copy_only
    payload["operations"] = operations
    return payload
