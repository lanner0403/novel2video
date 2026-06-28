@echo off
chcp 65001 >nul
REM Start the Novel-to-Reel pipeline server on Windows
pushd "%~dp0backend"

set PY=python
where python >nul 2>nul || set PY=py

%PY% -m pip install -q -r requirements.txt

echo.
echo Server running at http://127.0.0.1:8000   (press Ctrl+C to stop)
echo.
%PY% -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

popd
