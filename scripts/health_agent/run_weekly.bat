@echo off
REM Entry point for Windows Task Scheduler.
REM Runs the weekly health agent and logs stdout/stderr to scripts\health_agent\last_run.log.
REM
REM Adjust PYTHON_EXE below if your python lives elsewhere.

setlocal
set REPO=%~dp0..\..
set PYTHON_EXE=python

pushd "%REPO%"
"%PYTHON_EXE%" -m scripts.health_agent.run_weekly > "%~dp0last_run.log" 2>&1
set EXITCODE=%ERRORLEVEL%
popd

exit /b %EXITCODE%
