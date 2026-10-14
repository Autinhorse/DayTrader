@echo off
rem Quality gate: ruff, pyright, pytest, import-linter must all pass (DESIGN.md section 13)
cd /d "%~dp0"
uv run ruff check . || exit /b 1
uv run ruff format --check . || exit /b 1
uv run python -m pyright || exit /b 1
uv run pytest || exit /b 1
uv run lint-imports || exit /b 1
echo.
echo All checks passed.
