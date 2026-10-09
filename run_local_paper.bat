@echo off
rem Local paper trading: IBKR realtime data + local simulated fills (no broker orders). Engine window + UI window.
cd /d "%~dp0"
start "DayTrader local_paper" uv run --no-sync trader-live --profile local_paper --ui
