@echo off
rem JupyterLab for the notebook research interface
cd /d "%~dp0"
uv run --no-sync jupyter lab user/notebooks
