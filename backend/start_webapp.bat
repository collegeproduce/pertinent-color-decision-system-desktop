@echo off
echo ================================================================
echo  Pertinent Color Decision System - Web App Launcher
echo ================================================================
echo.

echo Starting Backend Server (Flask)...
cd backend
start cmd /k "python app.py"
cd ..

timeout /t 3 /nobreak >nul

echo Starting Frontend Server (React)...
cd frontend
start cmd /k "npm run dev"
cd ..

echo.
echo ================================================================
echo  Both servers are starting in separate windows...
echo  Backend: http://localhost:5000
echo  Frontend: http://localhost:3000
echo ================================================================
echo.
echo  Open your browser to: http://localhost:3000
echo.
pause
