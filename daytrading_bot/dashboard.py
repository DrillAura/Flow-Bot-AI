from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def load_supervisor_state_payload(state_path: Path) -> dict[str, Any]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    summary = payload.get("daily_summary") or {}
    history_progress = payload.get("history_progress") or {}
    if not summary:
        summary = {
            "generated_at": payload.get("updated_at"),
            "supervisor_status": payload.get("status", "unknown"),
            "progress_pct": history_progress.get("progress_pct"),
            "available_days": history_progress.get("available_days"),
            "required_days": history_progress.get("required_days"),
            "eta": history_progress.get("estimated_ready_at"),
            "last_errors": [],
            "gate_status": "pending",
            "gate_ready": None,
            "gate_blockers": [],
            "paper_forward_status": "idle",
        }
        payload["daily_summary"] = summary
    return payload


def write_supervisor_dashboard(output_path: Path, payload: dict[str, Any], refresh_seconds: int = 60) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_supervisor_dashboard_html(payload, refresh_seconds=refresh_seconds), encoding="utf-8")
    return output_path


def render_supervisor_dashboard_html(payload: dict[str, Any], refresh_seconds: int = 60) -> str:
    summary = payload.get("daily_summary") or {}
    history_progress = payload.get("history_progress") or {}
    last_prepare = payload.get("last_prepare_report") or {}
    capture = last_prepare.get("capture_report") or {}
    final_history = capture.get("final_history_status") or {}
    pair_status = final_history.get("pair_status") or {}
    optimization = last_prepare.get("walk_forward_optimization") or {}
    gate = last_prepare.get("paper_forward_gate") or {}
    forward = gate.get("forward_report") or {}
    forward_gates = forward.get("gates") or {}
    e2e_results = gate.get("e2e_results") or []
    launch = payload.get("launch_report") or {}

    status = str(payload.get("status", "unknown"))
    progress_pct = _fmt_pct(summary.get("progress_pct"))
    progress_bar_pct = float(summary.get("progress_pct") or 0.0)
    available_days = _fmt_num(summary.get("available_days"))
    required_days = summary.get("required_days")
    eta = _fmt_text(summary.get("eta"))
    last_errors = summary.get("last_errors") or ["none"]
    blockers = summary.get("gate_blockers") or ["none"]
    gate_status = str(summary.get("gate_status", "pending"))
    paper_forward_status = str(summary.get("paper_forward_status", "idle"))

    pair_rows = "".join(
        f"""
        <tr>
          <td>{html.escape(symbol)}</td>
          <td>{int(values.get('candles_1m', 0))}</td>
          <td>{int(values.get('candles_15m', 0))}</td>
          <td>{_fmt_num(values.get('span_days'))}</td>
          <td>{html.escape(str(values.get('last_ts', 'n/a')))}</td>
        </tr>
        """
        for symbol, values in sorted(pair_status.items())
    ) or '<tr><td colspan="5">No pair history loaded.</td></tr>'

    gate_rows = "".join(
        f"""
        <tr>
          <td>{html.escape(str(name))}</td>
          <td><span class="badge {_status_class(bool(values.get('passed')))}">{'pass' if values.get('passed') else 'fail'}</span></td>
          <td>{html.escape(str(values.get('actual')))}</td>
          <td>{html.escape(str(values.get('threshold')))}</td>
        </tr>
        """
        for name, values in sorted(forward_gates.items())
    ) or '<tr><td colspan="4">No forward gate data yet.</td></tr>'

    e2e_rows = "".join(
        f"""
        <tr>
          <td>{html.escape(str(result.get('name', 'unknown')))}</td>
          <td><span class="badge {_status_class(bool(result.get('ok')))}">{'pass' if result.get('ok') else 'fail'}</span></td>
          <td>{html.escape(str(result.get('details', '')))}</td>
        </tr>
        """
        for result in e2e_results
    ) or '<tr><td colspan="3">No E2E results yet.</td></tr>'

    error_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in last_errors)
    blocker_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in blockers)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{max(refresh_seconds, 5)}">
  <title>Supervisor Dashboard</title>
  <style>
    :root {{
      --bg: #f3efe4;
      --bg-accent: #fff9ef;
      --panel: rgba(255,255,255,0.82);
      --panel-strong: rgba(255,255,255,0.92);
      --text: #1f2b27;
      --muted: #5f6b65;
      --line: rgba(31,43,39,0.12);
      --good: #0f7b4d;
      --warn: #b7791f;
      --bad: #b33a3a;
      --idle: #5b6c84;
      --shadow: 0 18px 48px rgba(44, 55, 52, 0.10);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      background:
        radial-gradient(circle at top left, #d8e7db 0%, rgba(216,231,219,0) 34%),
        radial-gradient(circle at top right, #f2dfbf 0%, rgba(242,223,191,0) 28%),
        linear-gradient(180deg, var(--bg-accent) 0%, var(--bg) 100%);
      font-family: Aptos, "Segoe UI", "Helvetica Neue", sans-serif;
    }}
    .page {{
      width: min(1200px, calc(100vw - 32px));
      margin: 28px auto 40px;
    }}
    .hero {{
      padding: 24px 26px;
      border: 1px solid var(--line);
      border-radius: 24px;
      background: linear-gradient(135deg, rgba(255,255,255,0.94), rgba(255,248,234,0.86));
      box-shadow: var(--shadow);
    }}
    .hero h1 {{
      margin: 0 0 8px;
      font-size: clamp(28px, 4vw, 44px);
      line-height: 1.05;
      letter-spacing: -0.03em;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      max-width: 72ch;
    }}
    .status-row, .grid {{
      display: grid;
      gap: 16px;
      margin-top: 18px;
    }}
    .status-row {{
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
    }}
    .grid {{
      grid-template-columns: 1.15fr 0.85fr;
    }}
    .panel {{
      border: 1px solid var(--line);
      border-radius: 22px;
      background: var(--panel);
      box-shadow: var(--shadow);
      padding: 18px 18px 16px;
      backdrop-filter: blur(8px);
    }}
    .panel h2 {{
      margin: 0 0 12px;
      font-size: 18px;
      letter-spacing: -0.02em;
    }}
    .metric {{
      font-size: 28px;
      line-height: 1.05;
      margin: 4px 0;
      font-weight: 700;
      letter-spacing: -0.03em;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      background: #eef2f0;
      color: var(--idle);
    }}
    .badge.good {{ background: rgba(15,123,77,0.12); color: var(--good); }}
    .badge.warn {{ background: rgba(183,121,31,0.12); color: var(--warn); }}
    .badge.bad {{ background: rgba(179,58,58,0.12); color: var(--bad); }}
    .badge.idle {{ background: rgba(91,108,132,0.12); color: var(--idle); }}
    .progress {{
      height: 14px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(31,43,39,0.08);
      margin: 12px 0 8px;
    }}
    .progress > span {{
      display: block;
      height: 100%;
      width: {max(0.0, min(progress_bar_pct, 100.0)):.2f}%;
      background: linear-gradient(90deg, #b7791f, #0f7b4d);
    }}
    ul {{
      margin: 0;
      padding-left: 18px;
    }}
    li {{
      margin: 6px 0;
      color: var(--text);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
    }}
    th, td {{
      padding: 10px 8px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      font-size: 14px;
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      font-size: 11px;
    }}
    .two-col {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }}
    .mono {{
      font-family: "Cascadia Code", Consolas, monospace;
      font-size: 12px;
      word-break: break-all;
    }}
    @media (max-width: 900px) {{
      .grid, .two-col {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <span class="badge {_status_class_from_text(status)}">{html.escape(status.replace('_', ' '))}</span>
      <h1>Supervisor Dashboard</h1>
      <p>Read-only Übersicht für Fortschritt, OOS-Historie, Gate-Status und Paper-Forward-Runtime. Diese Seite visualisiert nur den aktuellen Supervisor-State.</p>
      <div class="status-row">
        <article class="panel">
          <div class="meta">Fortschritt</div>
          <div class="metric">{progress_pct}</div>
          <div class="meta">{available_days} / {html.escape(str(required_days or 'n/a'))} days</div>
          <div class="progress"><span></span></div>
        </article>
        <article class="panel">
          <div class="meta">ETA</div>
          <div class="metric" style="font-size:20px">{eta}</div>
          <div class="meta">Updated: {html.escape(str(payload.get('updated_at', 'n/a')))}</div>
        </article>
        <article class="panel">
          <div class="meta">Gate</div>
          <div class="metric" style="font-size:22px">{html.escape(gate_status)}</div>
          <div class="meta">Ready: {html.escape(str(summary.get('gate_ready')))}</div>
        </article>
        <article class="panel">
          <div class="meta">Paper Forward</div>
          <div class="metric" style="font-size:22px">{html.escape(paper_forward_status)}</div>
          <div class="meta">PID: {html.escape(str(payload.get('paper_forward_pid') or launch.get('pid') or 'n/a'))}</div>
        </article>
      </div>
    </section>

    <section class="grid">
      <article class="panel">
        <h2>Fehler und Blocker</h2>
        <div class="two-col">
          <div>
            <div class="meta">Letzte Fehler</div>
            <ul>{error_items}</ul>
          </div>
          <div>
            <div class="meta">Gate-Blocker</div>
            <ul>{blocker_items}</ul>
          </div>
        </div>
      </article>
      <article class="panel">
        <h2>Runtime</h2>
        <table>
          <tbody>
            <tr><th>Supervisor PID</th><td>{html.escape(str(payload.get('supervisor_pid') or 'n/a'))}</td></tr>
            <tr><th>Supervisor stop</th><td class="mono">{html.escape(str(payload.get('supervisor_stop_path', 'n/a')))}</td></tr>
            <tr><th>Paper stop</th><td class="mono">{html.escape(str(payload.get('paper_forward_stop_path', 'n/a')))}</td></tr>
            <tr><th>State path</th><td class="mono">{html.escape(str(payload.get('state_path', 'n/a')))}</td></tr>
            <tr><th>Dashboard path</th><td class="mono">{html.escape(str(payload.get('dashboard_path', 'n/a')))}</td></tr>
          </tbody>
        </table>
      </article>
    </section>

    <section class="grid">
      <article class="panel">
        <h2>Pair History</h2>
        <table>
          <thead>
            <tr><th>Pair</th><th>1m</th><th>15m</th><th>Days</th><th>Last Candle</th></tr>
          </thead>
          <tbody>{pair_rows}</tbody>
        </table>
      </article>
      <article class="panel">
        <h2>OOS Optimization</h2>
        <table>
          <tbody>
            <tr><th>Setup</th><td>{html.escape(str(optimization.get('setup_scope', 'n/a')))}</td></tr>
            <tr><th>Objective</th><td>{html.escape(str(optimization.get('objective', 'n/a')))}</td></tr>
            <tr><th>Variants tested</th><td>{html.escape(str(optimization.get('variants_tested', 'n/a')))}</td></tr>
            <tr><th>Eligible variants</th><td>{html.escape(str(optimization.get('eligible_variants', 'n/a')))}</td></tr>
            <tr><th>Zero-trade variants</th><td>{html.escape(str(optimization.get('zero_trade_variants', 'n/a')))}</td></tr>
            <tr><th>Insufficient history</th><td>{html.escape(str(optimization.get('insufficient_history', 'n/a')))}</td></tr>
          </tbody>
        </table>
      </article>
    </section>

    <section class="grid">
      <article class="panel">
        <h2>Forward Gates</h2>
        <table>
          <thead>
            <tr><th>Gate</th><th>Status</th><th>Actual</th><th>Threshold</th></tr>
          </thead>
          <tbody>{gate_rows}</tbody>
        </table>
      </article>
      <article class="panel">
        <h2>E2E Checks</h2>
        <table>
          <thead>
            <tr><th>Check</th><th>Status</th><th>Details</th></tr>
          </thead>
          <tbody>{e2e_rows}</tbody>
        </table>
      </article>
    </section>
  </main>
</body>
</html>
"""


def _fmt_num(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return html.escape(str(value))


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return html.escape(str(value))


def _fmt_text(value: Any) -> str:
    if value in (None, "", []):
        return "n/a"
    return html.escape(str(value))


def _status_class(value: bool) -> str:
    return "good" if value else "bad"


def _status_class_from_text(value: str) -> str:
    normalized = value.lower()
    if "started" in normalized or "running" in normalized or normalized == "green":
        return "good"
    if "wait" in normalized or "idle" in normalized or "pending" in normalized:
        return "warn"
    if "fail" in normalized or "stop" in normalized or "blocked" in normalized or normalized == "red":
        return "bad"
    return "idle"
