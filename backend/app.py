"""
Flask Web API for Pertinent Color Decision System

Provides REST API endpoints for the web frontend:
- PDF upload & processing
- Page preview generation
- Decision overrides
- JSON export
- Document management

Per ARCHITECTURE.md and README_WEBAPP.md specifications.
"""

import os
import uuid
import json
import io
import sys
import threading
from datetime import datetime
from pathlib import Path
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS
from werkzeug.utils import secure_filename
import pymupdf  # PyMuPDF
from PIL import Image

# Fix UTF-8 encoding for Windows console (handles emojis)
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, OSError):
        pass  # Silently skip if reconfigure not available

# Engine: CPCE v19 via adapter (drop-in for OptimizedColorPrintingPipeline)
from cpce_adapter import CPCEPipelineAdapter as OptimizedColorPrintingPipeline
from optimized_pipeline import OverrideManager
from models import PrintMode, MetadataSource
from csv_exporter import CSVExporter


# ============================================================
# Flask App Configuration
# ============================================================

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend access

# Configuration - Use temp directory for uploads in packaged app
import tempfile

# Detect if running as PyInstaller bundle
if getattr(sys, 'frozen', False):
    # Running as packaged app - use temp directory
    base_dir = Path(tempfile.gettempdir()) / 'PertinentColorApp'
else:
    # Running as script - use current directory
    base_dir = Path('.')

UPLOAD_FOLDER = base_dir / 'uploads'
RESULTS_FOLDER = base_dir / 'results'
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50MB
ALLOWED_EXTENSIONS = {'pdf'}

# Create folders
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
RESULTS_FOLDER.mkdir(parents=True, exist_ok=True)

print(f"📁 Upload folder: {UPLOAD_FOLDER}")
print(f"📁 Results folder: {RESULTS_FOLDER}")

app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_SIZE

# In-memory document cache
# Structure: {doc_id: {status, result, pages_data, summary, filename, filepath,
#                      total_pages, previews{page_id: png_bytes}, error, ...}}
document_cache = {}
cache_lock = threading.Lock()

# SSE listener queues — {doc_id: [Queue, Queue, ...]}
event_queues = {}

# Background workers
ENGINE_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="cpce-engine"
)
PREVIEW_EXECUTOR = ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="preview-render"
)

# Global instances
override_manager = OverrideManager()
csv_exporter = CSVExporter()


def push_event(doc_id, event_type, data=None):
    """Push an SSE event to every active listener for this doc."""
    with cache_lock:
        listeners = list(event_queues.get(doc_id, ()))
    for q in listeners:
        try:
            q.put_nowait({'type': event_type, 'data': data or {}})
        except Exception:
            pass


def render_previews_job(doc_id, filepath, total_pages):
    """Background job: render all page thumbnails as PNG bytes."""
    try:
        doc = pymupdf.open(filepath)
        for i in range(total_pages):
            with cache_lock:
                if doc_id not in document_cache:
                    doc.close()
                    return  # doc was cleared mid-render
            try:
                page = doc[i]
                pix = page.get_pixmap(dpi=72)
                png_bytes = pix.tobytes('png')
            except Exception as e:
                print(f"  [preview] page {i+1} render failed: {e}")
                continue
            with cache_lock:
                entry = document_cache.get(doc_id)
                if entry is None:
                    doc.close()
                    return
                entry['previews'][i + 1] = png_bytes
            push_event(doc_id, 'preview', {'page_id': i + 1})
        doc.close()
    except Exception as e:
        print(f"  [preview] {doc_id} fatal: {e}")
        push_event(doc_id, 'error', {'message': f'preview rendering failed: {e}'})


def process_engine_job(doc_id, filepath, filename):
    """Background job: run CPCE engine on the PDF (serialized — one at a time)."""
    started_at = datetime.now()
    try:
        with cache_lock:
            if doc_id not in document_cache:
                return  # cleared
            document_cache[doc_id]['status'] = 'analyzing'
        push_event(doc_id, 'status', {'status': 'analyzing'})

        pipeline = OptimizedColorPrintingPipeline(max_workers=8)
        result = pipeline.process_document(filepath, doc_id=doc_id)

        # Build pages_data with URL-based preview path (no inline base64).
        pages_data = []
        for page_record in result.pages:
            pages_data.append({
                'page_id': page_record.page_id,
                'decision': page_record.final_print_mode.value,
                'source': (
                    page_record.metadata_source.value
                    if page_record.metadata_source else 'unknown'
                ),
                'reason': page_record.decision_details,
                'preview': f'/api/document/{doc_id}/preview/{page_record.page_id}.png',
                'overridden': False,
            })
        summary = calculate_summary(result)

        with cache_lock:
            entry = document_cache.get(doc_id)
            if entry is None:
                return
            entry['result'] = result
            entry['pages_data'] = pages_data
            entry['summary'] = summary
            entry['status'] = 'done'
            entry['processed'] = True
            entry['analysis_seconds'] = (datetime.now() - started_at).total_seconds()

        push_event(doc_id, 'analyzed', {'pages': pages_data, 'summary': summary})
        push_event(doc_id, 'done', {})
        print(
            f"✅ {filename}: {summary['color_pages']} color, "
            f"{summary['bw_pages']} B&W, "
            f"{entry['analysis_seconds']:.1f}s analysis"
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        with cache_lock:
            entry = document_cache.get(doc_id)
            if entry is not None:
                entry['status'] = 'error'
                entry['error'] = str(e)
        push_event(doc_id, 'error', {'message': str(e)})


# ============================================================
# Helper Functions
# ============================================================

def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def generate_thumbnail(page, dpi=72):
    """
    Generate base64-encoded thumbnail for a page.
    
    Args:
        page: PyMuPDF page object
        dpi: Resolution for thumbnail
        
    Returns:
        Base64-encoded PNG image string
    """
    try:
        # Render page to pixmap
        pix = page.get_pixmap(dpi=dpi)
        
        # Convert to PNG bytes
        img_data = pix.tobytes("png")
        
        # Convert to base64
        import base64
        img_base64 = base64.b64encode(img_data).decode('utf-8')
        
        return f"data:image/png;base64,{img_base64}"
    
    except Exception as e:
        print(f"Error generating thumbnail: {e}")
        return None


def calculate_summary(result):
    """
    Calculate summary statistics from DocumentResult.
    
    Returns:
        Dictionary with summary stats
    """
    total_pages = result.total_pages
    color_pages = len(result.get_color_pages())
    bw_pages = len(result.get_bw_pages())
    
    # Calculate efficiency (early elimination %)
    bw_guaranteed = len([p for p in result.pages if p.bw_guaranteed])
    efficiency = (bw_guaranteed / total_pages * 100) if total_pages > 0 else 0
    
    return {
        'total_pages': total_pages,
        'color_pages': color_pages,
        'bw_pages': bw_pages,
        'efficiency': efficiency
    }


def format_page_data(page_record, preview_url):
    """
    Format PageRecord for frontend consumption.
    
    Args:
        page_record: PageRecord object
        preview_url: Base64 thumbnail URL
        
    Returns:
        Dictionary with page data for frontend
    """
    return {
        'page_id': page_record.page_id,
        'decision': page_record.final_print_mode.value,
        'source': page_record.metadata_source.value if page_record.metadata_source else 'unknown',
        'reason': page_record.decision_details,
        'preview': preview_url,
        'overridden': False  # Will be set to True if user overrides
    }


# ============================================================
# API Endpoints
# ============================================================

@app.route('/api/upload', methods=['POST'])
def upload_pdf():
    """
    Async upload — returns immediately with a doc_id. Engine runs on a background
    worker; preview thumbnails render in parallel. The client subscribes to
    /api/document/<doc_id>/events (SSE) for incremental progress.
    """
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        file = request.files['file']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        if not allowed_file(file.filename):
            return jsonify({'error': 'Only PDF files are allowed'}), 400

        doc_id = str(uuid.uuid4())
        filename = secure_filename(file.filename)
        filepath = UPLOAD_FOLDER / f"{doc_id}_{filename}"
        file.save(str(filepath))

        # Open the PDF briefly to get the page count for the initial response.
        try:
            with pymupdf.open(str(filepath)) as doc:
                total_pages = len(doc)
        except Exception as e:
            return jsonify({'error': f'Could not read PDF: {e}'}), 400

        print(f"\n{'='*70}\nQueued upload: {filename}  ({total_pages} pages)\n"
              f"Document ID: {doc_id}\n{'='*70}\n")

        with cache_lock:
            document_cache[doc_id] = {
                'doc_id': doc_id,
                'filename': filename,
                'filepath': str(filepath),
                'uploaded_at': datetime.now().isoformat(),
                'status': 'queued',
                'total_pages': total_pages,
                'previews': {},
                'pages_data': None,
                'summary': None,
                'result': None,
                'processed': False,
            }
            event_queues.setdefault(doc_id, [])

        # Kick off both jobs. Preview rendering and engine analysis run in parallel.
        PREVIEW_EXECUTOR.submit(render_previews_job, doc_id, str(filepath), total_pages)
        ENGINE_EXECUTOR.submit(process_engine_job, doc_id, str(filepath), filename)

        return jsonify({
            'doc_id': doc_id,
            'filename': filename,
            'status': 'queued',
            'total_pages': total_pages,
            'summary': {
                'total_pages': total_pages,
                'color_pages': 0,
                'bw_pages': 0,
                'efficiency': 0,
            },
            'pages': [],
        }), 202

    except Exception as e:
        print(f"\n❌ Error queueing upload: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Failed to queue PDF: {e}'}), 500


@app.route('/api/document/<doc_id>/preview/<int:page_id>.png', methods=['GET'])
def get_preview(doc_id, page_id):
    """Serve a rendered page thumbnail as PNG."""
    with cache_lock:
        entry = document_cache.get(doc_id)
        png_bytes = entry['previews'].get(page_id) if entry else None
    if png_bytes is None:
        return ('', 404)
    return Response(
        png_bytes,
        mimetype='image/png',
        headers={'Cache-Control': 'public, max-age=31536000, immutable'},
    )


@app.route('/api/document/<doc_id>/events', methods=['GET'])
def stream_events(doc_id):
    """Server-Sent Events stream for per-doc progress events."""
    with cache_lock:
        if doc_id not in document_cache:
            return ('', 404)
        entry = document_cache[doc_id]
        q = Queue()
        event_queues.setdefault(doc_id, []).append(q)

        # Replay current state for late subscribers so the UI catches up
        # without missing events.
        replay = [
            ('meta', {
                'doc_id': doc_id,
                'filename': entry['filename'],
                'total_pages': entry['total_pages'],
                'status': entry['status'],
            })
        ]
        for pid in sorted(entry['previews'].keys()):
            replay.append(('preview', {'page_id': pid}))
        if entry.get('pages_data') is not None:
            replay.append(('analyzed', {
                'pages': entry['pages_data'],
                'summary': entry['summary'],
            }))
        if entry['status'] == 'done':
            replay.append(('done', {}))
        elif entry['status'] == 'error':
            replay.append(('error', {'message': entry.get('error', 'unknown')}))

    def gen():
        try:
            for evt_type, data in replay:
                yield f"event: {evt_type}\ndata: {json.dumps(data)}\n\n"
            terminal = ('done', 'error')
            # If state was already terminal at subscription time, exit cleanly.
            if any(t in terminal for t, _ in replay):
                return
            while True:
                try:
                    evt = q.get(timeout=15.0)
                except Empty:
                    yield ": keepalive\n\n"
                    continue
                yield (
                    f"event: {evt['type']}\n"
                    f"data: {json.dumps(evt['data'])}\n\n"
                )
                if evt['type'] in terminal:
                    break
        finally:
            with cache_lock:
                listeners = event_queues.get(doc_id, [])
                if q in listeners:
                    listeners.remove(q)

    return Response(
        stream_with_context(gen()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


@app.route('/api/document/<doc_id>', methods=['GET'])
def get_document(doc_id):
    """
    Get current document state. Works regardless of processing status —
    returns whatever is available so far (rendered thumbnails, pages_data
    if analysis is done, summary, status).
    """
    try:
        with cache_lock:
            if doc_id not in document_cache:
                return jsonify({'error': 'Document not found'}), 404
            doc_data = document_cache[doc_id]
            response_data = {
                'doc_id': doc_id,
                'filename': doc_data['filename'],
                'status': doc_data.get('status', 'unknown'),
                'total_pages': doc_data.get('total_pages'),
                'summary': doc_data.get('summary') or {
                    'total_pages': doc_data.get('total_pages', 0),
                    'color_pages': 0,
                    'bw_pages': 0,
                    'efficiency': 0,
                },
                'pages': doc_data.get('pages_data') or [],
                'rendered_pages': sorted(doc_data.get('previews', {}).keys()),
                'error': doc_data.get('error'),
            }
        return jsonify(response_data), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/document/<doc_id>/page/<int:page_id>/override', methods=['POST'])
def override_decision(doc_id, page_id):
    """
    Override a page's color decision with tracking.
    
    Request:
        {
            "decision": "Color" | "B&W",
            "reason": "User manual override"
        }
        
    Response:
        {
            "success": true,
            "summary": {...}  // Updated summary
        }
    """
    try:
        with cache_lock:
            if doc_id not in document_cache:
                return jsonify({'error': 'Document not found'}), 404
            doc_data = document_cache[doc_id]
            if doc_data.get('status') != 'done':
                return jsonify({
                    'error': 'Document still processing',
                    'status': doc_data.get('status'),
                }), 409

        data = request.get_json()
        new_decision = data.get('decision')
        reason = data.get('reason', 'User manual override')

        if new_decision not in ['Color', 'B&W']:
            return jsonify({'error': 'Invalid decision. Must be "Color" or "B&W"'}), 400

        result = doc_data['result']
        pages_data = doc_data['pages_data']
        
        # Find and update the page
        page_found = False
        original_decision = None
        
        for page_record in result.pages:
            if page_record.page_id == page_id:
                # Save original decision
                original_decision = page_record.final_print_mode.value
                
                # Update page record
                if new_decision == 'Color':
                    page_record.final_print_mode = PrintMode.COLOR
                    page_record.decision_details = f"OVERRIDE: {reason}"
                else:
                    page_record.final_print_mode = PrintMode.BW
                    page_record.decision_details = f"OVERRIDE: {reason}"
                
                page_found = True
                break
        
        if not page_found:
            return jsonify({'error': f'Page {page_id} not found'}), 404
        
        # Track override in override manager
        override_manager.add_override(
            doc_id=doc_id,
            page_id=page_id,
            from_decision=original_decision,
            to_decision=new_decision,
            reason=reason
        )
        
        # Update pages_data
        for page_data in pages_data:
            if page_data['page_id'] == page_id:
                page_data['decision'] = new_decision
                page_data['reason'] = f"OVERRIDE: {reason}"
                page_data['overridden'] = True
                break
        
        # Recalculate summary
        summary = calculate_summary(result)
        doc_data['summary'] = summary
        
        print(f"✏️  Override: Page {page_id} → {new_decision}")
        print(f"   Original: {original_decision} → New: {new_decision}")
        print(f"   Updated summary: {summary['color_pages']} color, {summary['bw_pages']} B&W\n")
        
        return jsonify({
            'success': True,
            'summary': summary
        }), 200
    
    except Exception as e:
        print(f"❌ Error overriding decision: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/document/<doc_id>/export', methods=['GET'])
def export_results(doc_id):
    """
    Export document results as JSON file.
    
    Response:
        JSON file download with complete results
    """
    try:
        with cache_lock:
            if doc_id not in document_cache:
                return jsonify({'error': 'Document not found'}), 404
            doc_data = document_cache[doc_id]
            if doc_data.get('status') != 'done' or doc_data.get('result') is None:
                return jsonify({
                    'error': 'Document still processing',
                    'status': doc_data.get('status'),
                }), 409
            result = doc_data['result']

        # Generate export data (without base64 previews)
        export_data = {
            'document_id': doc_id,
            'filename': doc_data['filename'],
            'processed_at': doc_data['uploaded_at'],
            'summary': doc_data['summary'],
            'pages': [
                {
                    'page_id': page_record.page_id,
                    'final_print_mode': page_record.final_print_mode.value,
                    'decision_basis': page_record.metadata_source.value if page_record.metadata_source else 'unknown',
                    'details': page_record.decision_details
                }
                for page_record in result.pages
            ],
            'color_pages': result.get_color_pages(),
            'bw_pages': result.get_bw_pages()
        }
        
        # Save to file
        export_path = RESULTS_FOLDER / f"{doc_id}_results.json"
        with open(export_path, 'w', encoding='utf-8') as f:
            json.dump(export_data, f, indent=2)
        
        print(f"📥 Exported results to: {export_path}\n")
        
        # Send file
        return send_file(
            str(export_path),
            mimetype='application/json',
            as_attachment=True,
            download_name=f"{doc_id}_results.json"
        )
    
    except Exception as e:
        print(f"❌ Error exporting results: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/document/<doc_id>/clear', methods=['DELETE'])
def clear_document(doc_id):
    """
    Clear document from cache and delete uploaded file.
    
    Response:
        {
            "success": true,
            "message": "Document cleared"
        }
    """
    try:
        with cache_lock:
            if doc_id not in document_cache:
                return jsonify({'error': 'Document not found'}), 404
            doc_data = document_cache[doc_id]
            filepath = Path(doc_data['filepath'])
            del document_cache[doc_id]
            # Signal any active SSE listeners to drop the connection cleanly,
            # then forget them.
            listeners = event_queues.pop(doc_id, [])

        for q in listeners:
            try:
                q.put_nowait({'type': 'done', 'data': {'cleared': True}})
            except Exception:
                pass

        if filepath.exists():
            try:
                filepath.unlink()
                print(f"🗑️  Deleted file: {filepath}")
            except Exception as e:
                print(f"⚠️  Could not delete {filepath}: {e}")

        override_manager.clear_document(doc_id)
        print(f"✓ Cleared document: {doc_id}\n")

        return jsonify({
            'success': True,
            'message': 'Document cleared'
        }), 200

    except Exception as e:
        print(f"❌ Error clearing document: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'Pertinent Color Decision API',
        'version': '1.0.0',
        'documents_cached': len(document_cache)
    }), 200


@app.route('/api/documents', methods=['GET'])
def get_all_documents():
    """
    Get list of all documents in cache.
    
    Response:
        {
            "documents": [
                {
                    "doc_id": "...",
                    "filename": "...",
                    "summary": {...},
                    "uploaded_at": "...",
                    "processed": true
                },
                ...
            ]
        }
    """
    try:
        with cache_lock:
            documents = [
                {
                    'doc_id': doc_id,
                    'filename': doc_data['filename'],
                    'status': doc_data.get('status', 'unknown'),
                    'total_pages': doc_data.get('total_pages'),
                    'summary': doc_data.get('summary'),
                    'uploaded_at': doc_data['uploaded_at'],
                    'processed': doc_data.get('status') == 'done',
                }
                for doc_id, doc_data in document_cache.items()
            ]
        return jsonify({'documents': documents}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/export/csv', methods=['POST'])
def export_all_to_csv():
    """
    Export all cached documents to CSV in required format.
    
    Request:
        {
            "doc_ids": ["id1", "id2", ...],  // Optional: specific docs, or all if omitted
            "filename": "results.csv"  // Optional: custom filename
        }
        
    Response:
        CSV file download
    """
    try:
        data = request.get_json() or {}
        doc_ids = data.get('doc_ids', list(document_cache.keys()))
        custom_filename = data.get('filename', 'pertinent_color_results.csv')
        
        # Create CSV exporter
        exporter = CSVExporter()
        
        # Add each document — skip any that are not yet finished analysing.
        for doc_id in doc_ids:
            with cache_lock:
                if doc_id not in document_cache:
                    continue
                doc_data = document_cache[doc_id]
                if doc_data.get('status') != 'done' or doc_data.get('result') is None:
                    print(f"  CSV: skipping {doc_data.get('filename')} (status={doc_data.get('status')})")
                    continue
                result = doc_data['result']
                filename = doc_data['filename']
            
            # Apply overrides before export
            result_with_overrides = override_manager.apply_overrides(doc_id, result)
            
            # Add to exporter
            exporter.add_document(
                filename=filename,
                result=result_with_overrides,
                doc_id=doc_id
            )
        
        # Generate CSV file
        export_path = RESULTS_FOLDER / custom_filename
        exporter.export_to_csv(str(export_path))
        
        # Get summary
        summary = exporter.get_summary()
        
        print(f"\n📊 CSV Export Summary:")
        print(f"   Documents: {summary['total_documents']}")
        print(f"   Total pages: {summary['total_pages']}")
        print(f"   Color pages: {summary['total_color_pages']}")
        print(f"   B&W pages: {summary['total_bw_pages']}\n")
        
        # Send file
        return send_file(
            str(export_path),
            mimetype='text/csv',
            as_attachment=True,
            download_name=custom_filename
        )
    
    except Exception as e:
        print(f"❌ Error exporting CSV: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ============================================================
# Main Entry Point
# ============================================================

if __name__ == '__main__':
    print("\n" + "="*70)
    print("🎨 PERTINENT COLOR DECISION SYSTEM - WEB API")
    print("="*70)
    print("\nStarting Flask server...")
    print("Backend API: http://localhost:5000")
    print("Frontend should run on: http://localhost:3000")
    print("\nPress Ctrl+C to stop the server")
    print("="*70 + "\n")
    
    # In a PyInstaller bundle, Flask's debug reloader spawns a second copy of the
    # whole 200MB exe and the parent/child race for port 5000. Disable both in
    # frozen builds; keep them on for plain `python app.py` development.
    is_frozen = getattr(sys, 'frozen', False)

    # Run Flask server
    app.run(
        host='127.0.0.1' if is_frozen else '0.0.0.0',
        port=5000,
        debug=not is_frozen,
        use_reloader=not is_frozen,
        threaded=True,
    )
