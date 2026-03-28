from __future__ import annotations

import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from daytrading_bot.config import BotConfig
from daytrading_bot.dashboard_app import serve_dashboard_app

APP_NAME = "Flow Bot Dashboard"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
IDLE_SHUTDOWN_SECONDS = 300
CURRENT_PROJECT_ROOT = Path(r"C:\Users\Home\Desktop\Flow Bot AI - Trader AI Agent")


def main() -> int:
    project_root = resolve_project_root()
    logs_root = project_root / "logs" / "ops"
    data_dir = project_root / "data"
    logs_root.mkdir(parents=True, exist_ok=True)
    log_path = logs_root / "dashboard_desktop.log"
    configure_logging(log_path)
    logging.info("Starting %s", APP_NAME)
    logging.info("project_root=%s", project_root)
    logging.info("data_dir=%s", data_dir)
    logging.info("logs_root=%s", logs_root)

    host = os.environ.get("FLOWBOT_DASHBOARD_HOST", DEFAULT_HOST)
    preferred_port = int(os.environ.get("FLOWBOT_DASHBOARD_PORT", str(DEFAULT_PORT)))
    browser_enabled = os.environ.get("FLOWBOT_DASHBOARD_NO_BROWSER", "0").lower() not in {"1", "true", "yes"}

    port, reused = resolve_dashboard_port(host, preferred_port)
    url = f"http://{host}:{port}/"
    if reused:
        logging.info("Reusing existing dashboard instance at %s", url)
        if browser_enabled:
            webbrowser.open(url)
        return 0

    bot_config = BotConfig()
    try:
        server, url = serve_dashboard_app(
            bot_config=bot_config,
            data_dir=data_dir,
            logs_root=logs_root,
            host=host,
            port=port,
            open_browser=False,
            idle_shutdown_seconds=IDLE_SHUTDOWN_SECONDS,
        )
    except Exception:
        logging.exception("Failed to start dashboard server")
        raise

    logging.info("Dashboard listening on %s", url)
    if browser_enabled:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info("KeyboardInterrupt received, shutting down dashboard")
    except Exception:
        logging.exception("Dashboard server crashed")
        raise
    finally:
        server.server_close()
        logging.info("Dashboard server closed")
    return 0


def resolve_project_root() -> Path:
    env_root = os.environ.get("FLOWBOT_PROJECT_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser()
        if candidate.exists():
            return candidate.resolve()

    config_path = Path(sys.executable).with_suffix(".json") if getattr(sys, "frozen", False) else Path(__file__).resolve().with_name("desktop_dashboard.json")
    if config_path.exists():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            candidate = Path(payload.get("project_root", "")).expanduser()
            if candidate.exists():
                return candidate.resolve()
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass

    if CURRENT_PROJECT_ROOT.exists():
        return CURRENT_PROJECT_ROOT.resolve()

    fallback = Path.home() / "Desktop" / "Flow Bot AI - Trader AI Agent"
    if fallback.exists():
        return fallback.resolve()

    raise FileNotFoundError("Could not resolve Flow Bot project root.")


def resolve_dashboard_port(host: str, preferred_port: int, max_offset: int = 20) -> tuple[int, bool]:
    for offset in range(max_offset + 1):
        port = preferred_port + offset
        if is_dashboard_alive(host, port):
            return port, True
        if is_port_free(host, port):
            return port, False
    raise RuntimeError("Could not find a free dashboard port.")


def is_dashboard_alive(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
        return False


def is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) != 0


def configure_logging(log_path: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )


if __name__ == "__main__":
    raise SystemExit(main())
