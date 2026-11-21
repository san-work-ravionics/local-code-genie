@echo off
rem genie launcher. Usage: genie <command> [args]   (genie --help)
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
python -m genie %*
