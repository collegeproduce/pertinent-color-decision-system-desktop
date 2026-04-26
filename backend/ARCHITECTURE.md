# System Architecture - Web Application

## Full Stack Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         USER BROWSER                            │
│                    http://localhost:3000                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ HTTP Requests (JSON)
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                    REACT FRONTEND (Vite)                        │
│                                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │ UploadZone   │  │  Dashboard   │  │  PageGrid    │         │
│  │ Component    │  │  Component   │  │  Component   │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
│                                                                  │
│  Features:                                                       │
│  • Drag & drop PDF upload                                       │
│  • Real-time processing status                                  │
│  • Page preview thumbnails                                      │
│  • Decision badges (Color/B&W)                                  │
│  • Override buttons                                             │
│  • Tooltip explanations                                         │
│  • Filter & export                                              │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ REST API Calls
                             │ (axios)
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                     FLASK BACKEND API                           │
│                    http://localhost:5000                        │
│                                                                  │
│  API Endpoints:                                                  │
│  ┌────────────────────────────────────────────────────┐        │
│  │ POST   /api/upload          → Process PDF          │        │
│  │ GET    /api/document/<id>   → Get results          │        │
│  │ POST   /api/.../override    → Override decision    │        │
│  │ GET    /api/.../export      → Export JSON          │        │
│  │ DELETE /api/.../clear       → Clear document       │        │
│  └────────────────────────────────────────────────────┘        │
│                                                                  │
│  • Multipart file upload                                        │
│  • Base64 thumbnail generation                                  │
│  • In-memory result caching                                     │
│  • CORS enabled                                                 │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ Direct Import
                             │
┌────────────────────────────▼────────────────────────────────────┐
│              EXISTING PYTHON PIPELINE                           │
│                                                                  │
│  ┌──────────────────────────────────────────────────┐          │
│  │  ColorPrintingDecisionPipeline                   │          │
│  │                                                   │          │
│  │  1. MetadataExtractor                            │          │
│  │     ├── Tier1ColorspaceInspector                │          │
│  │     ├── Tier2ImageMetadataInspector             │          │
│  │     └── Tier3RasterProbe                        │          │
│  │                                                   │          │
│  │  2. PertinentColorEvaluator                     │          │
│  │     ├── Stage 1: Structural Analysis            │          │
│  │     └── Stage 2: Spatial Heuristics             │          │
│  │                                                   │          │
│  │  3. DocumentResult Generation                    │          │
│  └──────────────────────────────────────────────────┘          │
│                                                                  │
│  Input:  PDF file path                                          │
│  Output: DocumentResult with PageRecords                        │
└─────────────────────────────────────────────────────────────────┘
```

---

## Data Flow Diagram

### Upload & Processing Flow

```
User Action                API                 Pipeline              Result
───────────               ───────             ─────────            ────────

  Drag PDF      ────>    POST /upload   ───>  Process PDF   ───>  Generate
                                               • Tier 1-3          thumbnails
                                               • Stage 1-2
                                               • Decisions

  [Spinner]               Save file            Create              Return JSON
                          Parse                PageRecords         with base64
                                                                   previews

  Display       <────    Response JSON  <───  DocumentResult  <───  Cache
  Results                                                           results
```

### Override Flow

```
User Action                API                 Cache               Result
───────────               ───────             ─────────           ────────

  Click         ────>    POST /override  ───>  Update           ───>  Recalc
  "Change"                                      PageRecord             summary
                          {decision: "Color"}   in cache
                                                                       Return
  Update        <────    Response JSON   <───  Modified              updated
  Display                                       result                stats
```

### Export Flow

```
User Action                API                 File System         Result
───────────               ───────             ─────────           ────────

  Click         ────>    GET /export     ───>  Generate         ───>  Download
  "Export"                                      JSON file              JSON
                          
                          Strip previews       Save to
  Browser       <────    Send file       <───  results/              results
  Download               attachment                                   folder
```

---

## Technology Stack

### Frontend
```
React 18.2
├── Vite 5.0 (Build tool)
├── Axios (HTTP client)
└── CSS Modules (Styling)

Features:
• Component-based architecture
• Hooks (useState)
• CSS Grid & Flexbox
• Drag & drop API
• Base64 image rendering
```

### Backend
```
Flask 3.0
├── flask-cors (CORS support)
├── PyMuPDF (PDF processing)
└── Pillow (Image manipulation)

Features:
• RESTful API design
• Multipart form data
• In-memory caching
• Base64 encoding
• File serving
```

### Integration
```
Frontend ←──────→ Backend
  :3000      HTTP    :5000
         (JSON + Files)

Proxy: Vite proxies /api → localhost:5000
CORS: Enabled for local development
```

---

## File Structure

```
pertinentcolors/
│
├── backend/                      # Flask API Server
│   ├── app.py                    # Main Flask application
│   ├── requirements.txt          # Python dependencies
│   ├── uploads/                  # Uploaded PDFs (temp storage)
│   └── results/                  # Exported JSON files
│
├── frontend/                     # React Application
│   ├── src/
│   │   ├── App.jsx               # Main app component
│   │   ├── App.css               # Main styles
│   │   ├── main.jsx              # React entry point
│   │   ├── index.css             # Global styles
│   │   └── components/
│   │       ├── UploadZone.jsx    # PDF upload UI
│   │       ├── Dashboard.jsx     # Summary stats
│   │       ├── PageGrid.jsx      # Page list container
│   │       └── PageCard.jsx      # Individual page card
│   ├── index.html                # HTML entry point
│   ├── package.json              # npm dependencies
│   └── vite.config.js            # Vite config
│
├── main.py                       # Original CLI pipeline
├── models.py                     # Data models
├── metadata_extractor.py         # Metadata extraction
├── pertinent_color_evaluator.py  # Pertinence evaluation
├── tier1_tier2.py                # Tier 1 & 2 inspectors
├── tier3.py                      # Tier 3 raster probe
│
├── start_webapp.ps1              # Launch script
├── FRONTEND_SETUP.md             # Setup guide
└── README_WEBAPP.md              # Quick start guide
```

---

## API Contract

### POST /api/upload

**Request:**
```
Content-Type: multipart/form-data
Body: { file: <PDF binary> }
```

**Response:**
```json
{
  "doc_id": "document_name",
  "filename": "document.pdf",
  "summary": {
    "total_pages": 68,
    "color_pages": 12,
    "bw_pages": 56,
    "efficiency": 82.35
  },
  "pages": [
    {
      "page_id": 1,
      "decision": "Color",
      "source": "pertinence_rule",
      "reason": "Stage 2 PASS: Color area 2.45%...",
      "preview": "data:image/png;base64,...",
      "can_override": true,
      "overridden": false
    }
  ]
}
```

### POST /api/document/:id/page/:pageId/override

**Request:**
```json
{
  "decision": "Color",
  "reason": "User manual override"
}
```

**Response:**
```json
{
  "success": true,
  "page_id": 5,
  "new_decision": "Color",
  "summary": { /* updated stats */ }
}
```

---

## Security Considerations (For Production)

⚠️ **Current Implementation is LOCAL ONLY**

For production deployment, add:
- ✅ File upload validation
- ✅ File size limits (currently 50MB)
- ✅ MIME type verification
- ✅ Sanitized filenames
- ✅ Session management
- ✅ Rate limiting
- ✅ Authentication/Authorization
- ✅ HTTPS encryption
- ✅ Secure file storage
- ✅ Database instead of in-memory cache

---

## Performance Optimization

### Current Approach:
- **Thumbnails:** 150 DPI (good balance)
- **Base64:** Embedded in JSON (simple but large payload)
- **Caching:** In-memory (fast but not persistent)

### Future Improvements:
- [ ] Stream thumbnails separately
- [ ] Use Redis for caching
- [ ] Lazy load page previews
- [ ] WebSocket for progress updates
- [ ] Background job queue (Celery)

---

**Architecture designed for:**
- ✅ Simplicity (easy to understand)
- ✅ Separation of concerns
- ✅ Reusability (existing pipeline intact)
- ✅ Extensibility (easy to add features)
