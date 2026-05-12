@echo off
set "VENV_PY=%~dp0..\\.venv\\Scripts\\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" "%~dp0host.py"
) else (
    python "%~dp0host.py"
)
