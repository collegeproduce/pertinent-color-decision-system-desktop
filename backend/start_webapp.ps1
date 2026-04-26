# Pertinent Color Decision System - Web App Launcher
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host " Pertinent Color Decision System - Web App Launcher" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "Starting Backend Server (Flask)..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd backend; python app.py"

Start-Sleep -Seconds 3

Write-Host "Starting Frontend Server (React)..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd frontend; npm run dev"

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host " Both servers are starting in separate windows..." -ForegroundColor Green
Write-Host " Backend: http://localhost:5000" -ForegroundColor Green
Write-Host " Frontend: http://localhost:3000" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host ""
Write-Host " Open your browser to: http://localhost:3000" -ForegroundColor Cyan
Write-Host ""
