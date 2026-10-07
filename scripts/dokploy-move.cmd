@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0dokploy-move-ui.py"
) else (
  python "%~dp0dokploy-move-ui.py"
)
if errorlevel 1 pause
