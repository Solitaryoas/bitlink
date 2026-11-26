@echo off
setlocal
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
python3 -m bitlink %*
