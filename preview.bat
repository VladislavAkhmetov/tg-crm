@echo off
cd /d "%~dp0"
set "UGC_PREVIEW_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%UGC_PREVIEW_PYTHON%" (
  "%UGC_PREVIEW_PYTHON%" -c "import sys,runpy; sys.path[:0] = [r'venv\Lib\site-packages', r'tests']; runpy.run_path(r'tests\preview.py', run_name='__main__')"
) else (
  "venv\Scripts\python.exe" tests\preview.py
)
pause
