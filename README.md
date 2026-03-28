# Day-Trading Bot for 3Commas + Kraken

This repository contains a Python implementation of a low-frequency crypto day-trading bot designed for:

- `Kraken Spot`
- `3Commas Signal Bot`
- `100 EUR` starting capital
- `max 5%` account drawdown
- `Intraday breakout + pullback` tactics

## What is implemented

- Multi-timeframe strategy logic:
  - 15m regime classification (`bullish` / `recovery` / unsupported)
  - 5m breakout + pullback entry in bullish trends
  - 5m recovery reclaim entry in tightening recovery phases
  - recovery entries require a minimum setup score before they are even tradable
  - 1m shock-candle rejection
- Risk engine:
  - `0.9%` base risk per trade
  - max `3` trades per day
  - stop after `2` consecutive losses
  - stop after `1.8%` realized daily loss
  - drawdown ladder at `2.5%`, `3.5%`, `4.2%`, `5.0%`
- Session rules:
  - `08:00-11:30` Europe/Berlin
  - `14:30-18:30` Europe/Berlin
  - hard flat at `21:30`
- 3Commas webhook payload builder for:
  - `enter_long`
  - `exit_long`
  - `disable` / emergency market close
  - live recovery entries must meet the configured minimum quality gate
- Kraken public REST metadata client
- Kraken REST OHLC downloader
- Kraken WebSocket live scanner
- JSONL telemetry
- Offline backtest runner from local CSV data
- Backtest trade logs with:
  - per-setup expectancy
  - per-setup exit distributions
  - overall expectancy and exit distributions
- Separate interval history files for `1m` and `15m`
- Filter diagnostics with pass/fail/skip coverage per strategy gate
- Default Kraken EUR spot universe:
  - core: `XBTEUR`, `ETHEUR`, `SOLEUR`, `XRPEUR`, `LTCEUR`, `XDGEUR`
  - secondary liquid EUR pairs: `ADAEUR`, `LINKEUR`, `DOTEUR`, `TRXEUR`, `ATOMEUR`, `FETEUR`

## Project layout

- `daytrading_bot/config.py`: runtime configuration
- `daytrading_bot/models.py`: dataclasses and domain types
- `daytrading_bot/indicators.py`: technical indicator functions
- `daytrading_bot/strategy.py`: breakout + pullback strategy
- `daytrading_bot/risk.py`: drawdown and position sizing logic
- `daytrading_bot/execution.py`: 3Commas webhook client
- `daytrading_bot/engine.py`: orchestration for scanning, entries, exits
- `daytrading_bot/backtest.py`: offline backtest loop
- `daytrading_bot/history.py`: local history loading and timeframe alignment
- `daytrading_bot/cli.py`: CLI entrypoint

## Environment variables

- `THREE_COMMAS_SECRET`
- `THREE_COMMAS_BOT_UUID`
- `THREE_COMMAS_WEBHOOK_URL` (optional, defaults to `https://api.3commas.io/signal_bots/webhooks`)
- `BOT_MODE` (`paper` by default; use `live` only with explicit arming)
- `BOT_ALLOW_LIVE` (`false` by default; must be `true` before real 3Commas webhooks are allowed)
- `BOT_PAIRS` (optional comma-separated subset like `XBTEUR,ETHEUR,SOLEUR` to override the default pair universe for a specific runtime)

## Run tests

```powershell
python -m unittest discover -s tests -v
```

## Dry-run a sample payload

```powershell
python -m daytrading_bot.cli sample-entry --pair XBTEUR --price 35000 --stop 34600 --budget 80
```

## Run a local CSV backtest

Provide 1-minute OHLCV CSV files named:

- `XBTEUR.csv`
- `ETHEUR.csv`
- `SOLEUR.csv`

Optional dedicated 15-minute history files can also be stored as:

- `XBTEUR.15m.csv`
- `ETHEUR.15m.csv`
- `SOLEUR.15m.csv`

If a dedicated `15m` file is missing, the bot falls back to aggregating it from the `1m` file.

Each file must contain:

- `timestamp`
- `open`
- `high`
- `low`
- `close`
- `volume`

Then run:

```powershell
python -m daytrading_bot.cli backtest --data-dir .\\data
```

The backtest JSON now includes:

- `expectancy_eur`
- `expectancy_r`
- `exit_distribution`
- `setup_performance`
- detailed `trade_logs`

## Download real Kraken OHLC data

```powershell
python -m daytrading_bot.cli download-ohlc --data-dir .\\data --interval 1
python -m daytrading_bot.cli download-ohlc --data-dir .\\data --interval 15
```

## Merge the latest Kraken snapshot into local CSV history

```powershell
python -m daytrading_bot.cli sync-ohlc --data-dir .\\data --interval 1
python -m daytrading_bot.cli sync-ohlc --data-dir .\\data --interval 15
```

## Sweep the main strategy thresholds against local CSV history

```powershell
python -m daytrading_bot.cli calibrate --data-dir .\\data --top 5
```

The default fast profile now scans the active recovery setup parameters:

- `recovery_min_adx_15m`
- `recovery_max_ema_gap_pct`
- `recovery_compression_atr_multiple`
- `trail_activation_r`

Use `--profile full` only for the slower exhaustive sweep:

```powershell
python -m daytrading_bot.cli calibrate --data-dir .\\data --top 5 --profile full
```

Calibration scoring is now driven primarily by:

- `profit_factor`
- `expectancy_eur`
- `expectancy_r`

Trade count is only used as a sample-size penalty, not as the main objective.

## Diagnose why setups are blocked

```powershell
python -m daytrading_bot.cli diagnose-signals --data-dir .\\data
```

## See the first failing gate per pair and session

```powershell
python -m daytrading_bot.cli debug-signals --data-dir .\\data
```

## Keep local CSV history growing over repeated sync cycles

```powershell
python -m daytrading_bot.cli sync-ohlc-loop --data-dir .\\data --interval 1 --cycles 5 --sleep-seconds 60
```

## Capture history until the requested OOS window is available

This workflow keeps syncing `1m` and `15m` history until the requested `train/test`
window can actually support walk-forward research.

```powershell
python -m daytrading_bot.cli capture-until-ready --data-dir .\\data --train-days 10 --test-days 3 --poll-seconds 60
```

Use `--max-cycles` for a bounded run. `--max-cycles 0` means no cycle cap.

The report includes:

- `ready`
- `stopped_reason`
- `error_count`
- per-cycle sync results and history status snapshots

Transient Kraken timeouts are tolerated up to `--max-consecutive-errors`.

## Run OOS optimization directly against walk-forward folds

```powershell
python -m daytrading_bot.cli walk-forward-optimize --data-dir .\\data --setup both --profile fast --objective hybrid --train-days 10 --test-days 3 --top 5
```

This report ranks parameter variants by aggregated out-of-sample performance, not by a
single in-sample window.

## Full prep workflow before a new paper-forward phase

This command:

- captures history until ready
- runs walk-forward optimization
- then evaluates the paper-forward release gate

```powershell
python -m daytrading_bot.cli prepare-paper-forward --data-dir .\\data --setup both --profile fast --objective hybrid --train-days 10 --test-days 3 --top 3
```

## Long-running supervisor for the full next-step chain

This supervisor keeps checking history readiness, tolerates transient Kraken errors,
evaluates the OOS gate when ready, and only then starts the next paper forward run.
It can also run short paper research scans during open sessions so the Signal
Observatory and Shadow Portfolios collect real live-paper data in parallel.

```powershell
python -m daytrading_bot.cli paper-forward-supervisor --data-dir .\\data --setup both --profile fast --objective hybrid --train-days 10 --test-days 3 --top 3
```

Useful options:

- `--capture-poll-seconds`
- `--supervisor-poll-seconds`
- `--max-consecutive-errors`
- `--state-path`
- `--paper-forward-stdout-path`
- `--paper-forward-stderr-path`
- `--enable-research-scans`
- `--research-scan-available-eur`
- `--research-scan-duration-seconds`
- `--research-scan-max-messages`
- `--research-scan-min-interval-seconds`

Example with research scans enabled:

```powershell
python -m daytrading_bot.cli paper-forward-supervisor --data-dir .\\data --setup both --profile fast --objective hybrid --train-days 10 --test-days 3 --top 3 --enable-research-scans --research-scan-available-eur 100 --research-scan-duration-seconds 90 --research-scan-max-messages 60 --research-scan-min-interval-seconds 900
```

## Monitoring commandset

Read the current supervisor state, process liveness, and history ETA:

```powershell
python -m daytrading_bot.cli monitor-supervisor --state-path .\logs\ops\paper_forward_supervisor_YYYYMMDD_HHMMSS\supervisor_state.json
```

Useful raw file tails:

```powershell
Get-Content .\logs\ops\paper_forward_supervisor_YYYYMMDD_HHMMSS\supervisor_state.json -Wait
Get-Content .\logs\ops\paper_forward_supervisor_YYYYMMDD_HHMMSS\paper_forward_stdout.log -Wait
Get-Content .\logs\ops\paper_forward_supervisor_YYYYMMDD_HHMMSS\paper_forward_stderr.log -Wait
```

Each supervisor cycle now also refreshes these read-only monitoring artifacts automatically:

- `supervisor_daily_summary.json`
- `supervisor_daily_summary_YYYY-MM-DD.md`
- `supervisor_dashboard.html`

You can render the dashboard again on demand from any state file:

```powershell
python -m daytrading_bot.cli render-supervisor-dashboard --state-path .\logs\ops\paper_forward_supervisor_YYYYMMDD_HHMMSS\supervisor_state.json
```

The generated HTML dashboard is visualization-only. It does not change strategy, risk, or runtime behavior.

## Local monitoring web app

There is also a read-only local web app that sits on top of the same supervisor state and history files.
It is designed as an operator cockpit:

- dark, exchange-like visual density
- live OOS readiness progress
- ETA and collection speed
- watchdog / supervisor / paper-forward runtime state
- latest sync deltas per pair and interval
- recent run directories and artifact paths

Start it directly from the CLI:

```powershell
python -m daytrading_bot.cli serve-dashboard-app --data-dir .\data --logs-dir .\logs\ops --host 127.0.0.1 --port 8787
```

Then open:

- [http://127.0.0.1:8787/](http://127.0.0.1:8787/)

Convenience launchers are included:

- PowerShell:
  - `scripts\start_monitor_dashboard.ps1`
- Hidden desktop launcher:
  - `scripts\start_monitor_dashboard_desktop.ps1`
- CMD wrapper:
  - `scripts\run_monitor_dashboard.cmd`

Example:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_monitor_dashboard.ps1 -OpenBrowser
```

This web app is read-only. It does not change strategy, supervisor logic, risk, gate evaluation, or live/paper execution behavior.

The dashboard also exposes the research layer:

- Signal Observatory summaries from live-paper scans
- Shadow Portfolio comparisons for `50 / 100 / 250 / 500 / 1000 EUR`
- filters for individual shadow portfolios and regime rows
- clickable chart markers, journal drilldown, and CSV export

On Windows systems that block self-built unsigned executables, the recommended operator path is a desktop shortcut that launches the hidden PowerShell desktop launcher and then opens the browser. This preserves the same double-click workflow without changing the runtime itself.

## Keep the supervisor running automatically

For operator use there are now two additional layers:

1. `ensure-supervisor`
   - one-shot check
   - starts the supervisor if the state file is missing or the PID is dead
   - does nothing if the supervisor is already alive

```powershell
python -m daytrading_bot.cli ensure-supervisor --data-dir .\data --state-path .\logs\ops\paper_forward_supervisor_live\supervisor_state.json
```

2. `supervisor-watchdog`
   - long-running outer process
   - calls `ensure-supervisor` on a fixed interval
   - restarts the supervisor automatically after crashes or unexpected exits
   - the default launcher now enables short research scans during open sessions

```powershell
python -m daytrading_bot.cli supervisor-watchdog --data-dir .\data --state-path .\logs\ops\paper_forward_supervisor_live\supervisor_state.json --watchdog-poll-seconds 60
```

Windows helper scripts are included:

- Start watchdog in the background:
  - `scripts\start_supervisor_watchdog.ps1`
- Command wrapper for Task Scheduler:
  - `scripts\run_supervisor_watchdog.cmd`
- Register a Task Scheduler entry on logon:
  - `scripts\register_supervisor_watchdog_task.ps1`

Recommended setup:

1. use one fixed `state-path`
2. run the watchdog, not only the supervisor
3. if you want restart after reboot/logon, register the watchdog as a scheduled task

Note:

- On some Windows systems the task registration step requires an elevated PowerShell session.

## Stop workflow

Request a clean stop for the supervisor and the future paper-forward runtime:

```powershell
python -m daytrading_bot.cli stop-runtime --state-path .\logs\ops\paper_forward_supervisor_YYYYMMDD_HHMMSS\supervisor_state.json
```

Force terminate lingering processes after the grace window if needed:

```powershell
python -m daytrading_bot.cli stop-runtime --state-path .\logs\ops\paper_forward_supervisor_YYYYMMDD_HHMMSS\supervisor_state.json --grace-seconds 10 --force
```

## Run the live Kraken scanner in dry-run mode

```powershell
python -m daytrading_bot.cli live-scan --available-eur 100 --duration-seconds 30 --bootstrap-dir .\\data --mode paper
```

The `live-scan` command now prints a `preflight` section first. `--mode live` is blocked unless:

- `BOT_MODE=live`
- `BOT_ALLOW_LIVE=true`
- `THREE_COMMAS_SECRET` is set
- `THREE_COMMAS_BOT_UUID` is set

In addition, `recovery_reclaim` entries are blocked in live mode unless they satisfy the configured minimum live quality gate.

## Build a forward-test report from telemetry

```powershell
python -m daytrading_bot.cli forward-report
python -m daytrading_bot.cli forward-report --telemetry-path .\\logs\\forward_report_sample.jsonl
```

## Notes

- The bot is intentionally `spot-only`, `long-only`, and `single-position`.
- Live trading remains blocked until forward-test criteria are met.
- 3Commas payload fields follow the official Signal Bot Custom Signal JSON format.

## Multi-device workflow

This repo now separates `code` from `runtime`.

- GitHub should store:
  - source code
  - tests
  - scripts
  - docs
- Each device stores its own runtime locally under:
  - `.runtime/<device-id>/data`
  - `.runtime/<device-id>/logs`

That means:

- your desktop and laptop can run the same bot code
- both devices keep collecting their own history, telemetry, and supervisor state
- raw runtime data does not collide in Git

Resolve the current device runtime layout:

```powershell
python -m daytrading_bot.cli device-runtime
python -m daytrading_bot.cli device-runtime --device-id laptop-main
```

Copy the old top-level `data/` and `logs/` folders into the new per-device layout:

```powershell
python -m daytrading_bot.cli migrate-runtime-layout --project-root .
```

Move instead of copy if you explicitly want to retire the legacy folders:

```powershell
python -m daytrading_bot.cli migrate-runtime-layout --project-root . --move
```

Recommended GitHub flow:

1. Keep `.runtime/`, `data/`, and `logs/` out of Git.
2. Clone the repo on the laptop.
3. Set a unique device id per machine:
   - desktop example: `FLOW_BOT_DEVICE_ID=desktop-main`
   - laptop example: `FLOW_BOT_DEVICE_ID=laptop-main`
4. Start the watchdog/dashboard normally. The scripts now default to the per-device runtime folders.

Example laptop bootstrap:

```powershell
$env:FLOW_BOT_DEVICE_ID = "laptop-main"
python -m daytrading_bot.cli device-runtime
powershell -ExecutionPolicy Bypass -File .\scripts\start_supervisor_watchdog.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start_monitor_dashboard.ps1 -OpenBrowser
```

This is the intended sync model:

- GitHub = shared codebase and backup
- per-device runtime = local data collection and paper/live state
- later promotion decisions = based on summaries and shared code changes, not by merging raw CSV/log files blindly

## Git-safe device reports

Each device can now export a compact runtime summary into the tracked `reports/`
folder without pushing raw CSV history or local supervisor logs into Git.

Export a report for the current machine:

```powershell
python -m daytrading_bot.cli export-device-report --project-root .
```

Or explicitly for a named device id:

```powershell
python -m daytrading_bot.cli export-device-report --project-root . --device-id block30
```

This writes:

- `reports/devices/<device-id>/latest.json`
- `reports/devices/<device-id>/latest.md`

The exported report is designed to be committed to GitHub safely. It includes:

- active runtime paths
- latest supervisor state path
- history progress toward the `10d / 3d` OOS window
- gate and paper-forward status
- pair count
- strategy-lab champion and promotion reason
- forward summary metrics such as win rate, profit factor, drawdown, and net PnL

Recommended practice:

1. each device keeps raw runtime locally under `.runtime/<device-id>/...`
2. each device periodically runs `export-device-report`
3. only the compact report artifacts are shared via GitHub

## Bootstrap a second device

The repo can now prepare a new machine with device-specific runtime folders and
desktop launchers in one step.

Example for a laptop:

```powershell
python -m daytrading_bot.cli bootstrap-device --project-root . --device-id laptop-main --desktop-dir $HOME\Desktop
```

This creates desktop launchers such as:

- `Flow Bot Dashboard (laptop-main).cmd`
- `Flow Bot Watchdog (laptop-main).cmd`
- `Flow Bot Device Report (laptop-main).cmd`

There is also a PowerShell wrapper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_device.ps1 -ProjectRoot . -DeviceId laptop-main -DesktopDir $HOME\Desktop
```

Optional migration flags:

- `-MigrateLegacy`
- `-MoveLegacy`

Use them only if the target machine already has old top-level `data/` and `logs/`
folders that should be copied or moved into `.runtime/<device-id>/...`.

Suggested laptop setup flow:

1. clone the GitHub repo
2. set `FLOW_BOT_DEVICE_ID=laptop-main`
3. run `bootstrap-device`
4. start the watchdog from the generated desktop launcher
5. start the dashboard from the generated desktop launcher
6. periodically run the generated device-report launcher and commit the updated `reports/devices/laptop-main/*`
