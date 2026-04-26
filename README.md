# Pertinent Color Decision System

Windows desktop app that decides which pages of a legal PDF should print in colour vs B&W, page-by-page.

## Stack

- **Backend**: Python (Flask) + the CPCE v19 engine (vendored under `backend/cpce/`)
- **Frontend**: React + Vite, wrapped in Electron
- **Packaging**: PyInstaller (backend exe) → electron-packager → Inno Setup installer

## Build from source

### Prerequisites

- Python 3.13 in a virtualenv at `.venv/` (or wherever — see Dev notes)
- Node.js 18+
- (Optional) Tesseract-OCR 5.x at `backend/assets/Tesseract-OCR/` if you want OCR
  for image-only/scanned PDFs. Not bundled in source — download from
  <https://github.com/UB-Mannheim/tesseract/wiki> and copy the install folder
  into `backend/assets/`.
- Inno Setup 6 (only needed to build the installer)

### Backend

```
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r backend/requirements.txt
cd backend
python app.py                 # http://localhost:5000
```

### Frontend (dev)

```
cd frontend
npm install
npm run dev                   # http://localhost:3000 (proxies /api -> :5000)
```

### Production build

```
# 1. Backend exe
cd backend
python -m PyInstaller --name app --onefile \
  --add-data "*.py;." --add-data "cpce;cpce" --add-data "assets;assets" \
  --hidden-import=pymupdf --hidden-import=flask --hidden-import=flask_cors \
  --hidden-import=cv2 --hidden-import=scipy --hidden-import=sklearn \
  --hidden-import=rapidfuzz --hidden-import=pytesseract \
  --collect-all pymupdf --collect-all cv2 --collect-all scipy \
  --collect-all sklearn --collect-all rapidfuzz \
  app.py --distpath ../dist-backend --clean -y

# 2. Frontend + Electron package
cd ../frontend
npm run build
npx electron-packager . "Pertinent Color Decision System" \
  --platform=win32 --arch=x64 --out=build-final --overwrite \
  --ignore="node_modules|src|\.vite|dist-electron.*|build-.*|.*\.zip$"

# 3. Copy backend into the package
mkdir -p "build-final/Pertinent Color Decision System-win32-x64/resources/backend"
cp ../dist-backend/app.exe \
   "build-final/Pertinent Color Decision System-win32-x64/resources/backend/app.exe"

# 4. Build installer
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" ..\installer.iss
```

## Layout

```
backend/
  cpce/                       reasoning engine (vendored, do not modify)
  app.py                      Flask server: /api/upload, SSE, preview endpoints
  cpce_adapter.py             bridges CPCE DecisionResult to per-page JSON schema
  csv_exporter.py             BW / Color / Partial CSV export
  optimized_pipeline.py       legacy — kept for OverrideManager only
frontend/
  src/                        React UI
  electron/main.js            spawns the bundled backend, hosts the window
installer.iss                 Inno Setup script
```

## Dev notes

- The engine writes audit logs to `%TEMP%\PertinentColorApp\logs` — kept out of
  Program Files because that path is read-only without admin.
- Flask's debug reloader is gated on `getattr(sys, 'frozen', False)` so it only
  runs in `python app.py`, never inside the PyInstaller bundle.
- Don't modify `backend/cpce/`. Speed / accuracy improvements come from the
  adapter or from packaging, never from edits inside the engine.
