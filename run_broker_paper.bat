@echo off
rem IBKR paper account: places real orders in the PAPER account (Gateway: Read-Only API unchecked). Engine + UI.
cd /d "%~dp0"
start "DayTrader broker_paper" uv run --no-sync python -m trader.apps.live_app --profile broker_paper --ui
