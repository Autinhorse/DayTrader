@echo off
rem Research desktop app (charts, backtests, replay, data)
cd /d "%~dp0"
start "" uv run --no-sync pythonw -m trader.apps.research_app
