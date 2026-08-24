@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt -q
echo Abri http://127.0.0.1:8000 en el navegador
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
