@echo off
cd /d "%~dp0"
where docker >nul 2>&1
if %errorlevel%==0 (
  echo PropMap en http://127.0.0.1:8000
  docker compose up --build
  goto :eof
)
python -m pip install -r requirements.txt -q
echo Abri http://127.0.0.1:8000 en el navegador
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
