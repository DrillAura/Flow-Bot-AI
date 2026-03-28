from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .runtime_layout import RuntimePaths, build_runtime_paths, ensure_runtime_dirs, migrate_legacy_runtime


@dataclass(frozen=True)
class DeviceBootstrapReport:
    project_root: str
    device_id: str
    runtime_paths: RuntimePaths
    desktop_dir: str
    created_launchers: list[str]
    migration: dict[str, Any] | None


def resolve_desktop_dir() -> Path:
    return Path.home() / "Desktop"


def _write_cmd_launcher(path: Path, content: str) -> None:
    path.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def create_device_desktop_launchers(
    project_root: str | Path | None = None,
    device_id: str | None = None,
    desktop_dir: str | Path | None = None,
) -> list[str]:
    runtime_paths = ensure_runtime_dirs(build_runtime_paths(project_root=project_root, device_id=device_id))
    root = Path(runtime_paths.project_root)
    desktop = Path(desktop_dir) if desktop_dir is not None else resolve_desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)

    launchers = {
        desktop / f"Flow Bot Dashboard ({runtime_paths.device_id}).cmd": (
            f'@echo off\n'
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{root}\\scripts\\start_monitor_dashboard_desktop.ps1" '
            f'-ProjectRoot "{root}" -DeviceId "{runtime_paths.device_id}"\n'
        ),
        desktop / f"Flow Bot Watchdog ({runtime_paths.device_id}).cmd": (
            f'@echo off\n'
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{root}\\scripts\\start_supervisor_watchdog.ps1" '
            f'-ProjectRoot "{root}" -DeviceId "{runtime_paths.device_id}"\n'
        ),
        desktop / f"Flow Bot Device Report ({runtime_paths.device_id}).cmd": (
            f'@echo off\n'
            f'cd /d "{root}"\n'
            f'python -m daytrading_bot.cli export-device-report --project-root "{root}" --device-id "{runtime_paths.device_id}"\n'
            f'pause\n'
        ),
    }
    for path, content in launchers.items():
        _write_cmd_launcher(path, content)
    return [str(path) for path in launchers]


def bootstrap_device(
    project_root: str | Path | None = None,
    device_id: str | None = None,
    *,
    desktop_dir: str | Path | None = None,
    migrate_legacy: bool = False,
    move_legacy: bool = False,
) -> DeviceBootstrapReport:
    runtime_paths = ensure_runtime_dirs(build_runtime_paths(project_root=project_root, device_id=device_id))
    created = create_device_desktop_launchers(
        project_root=runtime_paths.project_root,
        device_id=runtime_paths.device_id,
        desktop_dir=desktop_dir,
    )
    migration = None
    if migrate_legacy:
        migration = migrate_legacy_runtime(
            project_root=runtime_paths.project_root,
            device_id=runtime_paths.device_id,
            copy_only=not move_legacy,
        )
    desktop = Path(desktop_dir) if desktop_dir is not None else resolve_desktop_dir()
    return DeviceBootstrapReport(
        project_root=runtime_paths.project_root,
        device_id=runtime_paths.device_id,
        runtime_paths=runtime_paths,
        desktop_dir=str(desktop),
        created_launchers=created,
        migration=migration,
    )


def bootstrap_device_payload(*args, **kwargs) -> dict[str, Any]:
    return asdict(bootstrap_device(*args, **kwargs))
