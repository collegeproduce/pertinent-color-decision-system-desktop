# Web Frontend Setup & Launch Guide

## Complete Web Application for Pertinent Color Decision System

**Stack:**
- **Backend:** Flask API (Python)
- **Frontend:** React + Vite
- **Communication:** REST API with JSON

---

## 🚀 Quick Start

### 1. Backend Setup (Terminal 1)

```powershell
# Navigate to backend folder
cd backend

# Install Python dependencies (if not already in venv)
pip install -r requirements.txt

# Start Flask server
python app.py
```

**Backend will run on:** `http://localhost:5000`

---

### 2. Frontend Setup (Terminal 2)

```powershell
# Navigate to frontend folder
cd frontend

# Install Node.js dependencies (first time only)
npm install

# Start development server
npm run dev
```

**Frontend will run on:** `http://localhost:3000`

---

## 📋 Prerequisites

### Required Software:
- ✅ **Python 3.8+** (already have with .venv)
- ✅ **Node.js 16+** (Download from: https://nodejs.org/)
- ✅ **npm** (comes with Node.js)

### Check Node.js Installation:
```powershell
node --version
npm --version
```

If not installed, download from: https://nodejs.org/

---

## 🎯 Usage Flow

1. **Start Backend** → Flask server processes PDFs
2. **Start Frontend** → Open browser to `http://localhost:3000`
3. **Upload PDF** → Drag & drop or click to browse
4. **Review Results** → See page-by-page decisions with previews
5. **Override Decisions** → Click "Change to Color/B&W" on any page
6. **Export Results** → Download JSON report

---

## 🖥️ Features Implemented

### Core Features (From Requirements)
✅ **Drag & Drop Upload** - Intuitive PDF upload  
✅ **Page Preview** - Thumbnail images for every page  
✅ **B&W/Color Decision** - Visual badges on each page  
✅ **Decision Override** - Toggle any page Color ↔ B&W  

### Additional UX Features
✅ **Progress Indicator** - Shows processing status  
✅ **Summary Dashboard** - Stats: total pages, color count, B&W count, efficiency  
✅ **Decision Tooltips** - Hover ℹ️ to see why decision was made  
✅ **Filter View** - Show All / Color Only / B&W Only  
✅ **Export Report** - Download JSON with all decisions  
✅ **Clear Document** - Start over with new PDF  
✅ **Override Indicator** - Visual marker (✏️) for manually changed pages  

---

## 📁 Project Structure

```
pertinentcolors/
├── backend/
│   ├── app.py                 # Flask API server
│   ├── requirements.txt       # Python dependencies
│   ├── uploads/              # Uploaded PDFs (auto-created)
│   └── results/              # Exported results (auto-created)
│
├── frontend/
│   ├── src/
│   │   ├── App.jsx           # Main application
│   │   ├── components/
│   │   │   ├── UploadZone.jsx    # Drag & drop upload
│   │   │   ├── Dashboard.jsx     # Summary statistics
│   │   │   ├── PageGrid.jsx      # Page grid container
│   │   │   └── PageCard.jsx      # Individual page card
│   │   └── index.css         # Global styles
│   ├── package.json          # Node dependencies
│   └── vite.config.js        # Vite configuration
│
├── main.py                   # Original CLI pipeline
├── models.py
├── metadata_extractor.py
├── pertinent_color_evaluator.py
└── ...
```

---

## 🔧 API Endpoints

### Backend REST API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/health` | GET | Health check |
| `/api/upload` | POST | Upload & process PDF |
| `/api/document/<doc_id>` | GET | Get document results |
| `/api/document/<doc_id>/page/<page_id>/override` | POST | Override page decision |
| `/api/document/<doc_id>/export` | GET | Export JSON report |
| `/api/document/<doc_id>/clear` | DELETE | Clear document |

---

## 🎨 UI Preview

### Upload Screen
- Large drag & drop zone
- File browser fallback
- Processing spinner

### Results Screen
- **Top:** Document name + summary stats (4 cards)
- **Filters:** All Pages / Color Only / B&W Only
- **Grid:** Page cards with:
  - Preview thumbnail
  - Color/B&W badge
  - Page number
  - Info tooltip (hover)
  - Override button

---

## 🐛 Troubleshooting

### Backend Won't Start
```powershell
# Make sure you're in the backend folder
cd backend

# Check if Flask is installed
pip list | findstr Flask

# Reinstall if needed
pip install -r requirements.txt
```

### Frontend Won't Start
```powershell
# Make sure Node.js is installed
node --version

# Clean install
cd frontend
rm -r node_modules
npm install
```

### CORS Errors
- Backend must run on port 5000
- Frontend must run on port 3000
- `flask-cors` must be installed

### Port Already in Use
```powershell
# Kill process on port 5000 (backend)
netstat -ano | findstr :5000
taskkill /PID <PID> /F

# Kill process on port 3000 (frontend)
netstat -ano | findstr :3000
taskkill /PID <PID> /F
```

---

## 🚢 Production Deployment (Future)

For production deployment, you'll need:

1. **Build Frontend:**
   ```bash
   cd frontend
   npm run build
   ```

2. **Serve Frontend from Flask:**
   - Copy `frontend/dist` to `backend/static`
   - Add route in Flask to serve `index.html`

3. **Use Production Server:**
   - Replace Flask dev server with `gunicorn` or `waitress`

4. **Environment Variables:**
   - Configure upload limits
   - Set secure secret keys
   - Add authentication

---

## 📝 Next Steps

1. ✅ Test with various PDFs
2. ⏳ Add Vision OCR Stage 3 (optional)
3. ⏳ Add print job splitter (separate B&W/Color PDFs)
4. ⏳ Add cost calculator
5. ⏳ Add user authentication (multi-user)

---

## 💡 Tips

- Keep both terminals open (backend + frontend)
- Backend must start BEFORE frontend
- Changes to React code hot-reload automatically
- Changes to Flask code require server restart

---

**Need Help?** Check the console output in both terminals for errors.
