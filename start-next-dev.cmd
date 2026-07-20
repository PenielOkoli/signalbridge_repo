@echo off
cd /d "%~dp0"
node node_modules\next\dist\bin\next dev > next-dev.log 2> next-dev.err.log
