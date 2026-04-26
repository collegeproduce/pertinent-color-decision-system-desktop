"""
CPCE v5 - FastAPI Application
RESTful API for the Contextual Pertinent Color Engine.
"""
from fastapi import FastAPI, File, UploadFile, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
import tempfile
import shutil
from pathlib import Path
import time

from .engine import CPCEEngine
from .pdf_processor import PDFProcessor
from .models import CPCEConfig, DecisionResult


app = FastAPI(
    title="CPCE v5 API",
    description="Contextual Pertinent Color Engine - Enterprise-grade PDF color analysis",
    version="5.0.0"
)

# Global engine instance
engine = CPCEEngine()
pdf_processor = PDFProcessor()


class AnalyzeRequest(BaseModel):
    case_id: Optional[str] = None
    dpi: int = 150
    config: Optional[Dict[str, Any]] = None
    # page_hints: per-page human override hints.
    # Keys are page indices (0-based) as strings (JSON constraint).
    # Values: "pertinent" | "decorative" | "review"
    # Example: {"0": "pertinent", "5": "decorative", "12": "review"}
    page_hints: Optional[Dict[str, str]] = None


class PageDecision(BaseModel):
    page_id: int
    should_use_color: bool
    final_score: float
    confidence: float
    is_override: bool
    override_reason: str
    reasoning: Dict[str, Any]
    processing_time_ms: float


class AnalysisResponse(BaseModel):
    case_id: str
    total_pages: int
    decisions: List[PageDecision]
    summary: Dict[str, Any]


@app.post("/analyze/pdf", response_model=AnalysisResponse)
async def analyze_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    case_id: Optional[str] = None,
    dpi: int = 150,
    page_hints: Optional[str] = None,  # JSON string: {"0": "pertinent", "5": "review"}
):
    """
    Analyze a PDF document to determine which pages need color.

    page_hints (optional): JSON string mapping page indices to hint types.
    Valid hint types: "pertinent", "decorative", "review".
    Example: '{"0": "pertinent", "5": "decorative"}'
    """
    if not pdf_processor.is_available():
        raise HTTPException(500, "PDF processing not available. Install PyMuPDF and OpenCV.")

    # Validate file
    if not file.filename.endswith('.pdf'):
        raise HTTPException(400, "Only PDF files are accepted")

    # Parse page_hints JSON string → {int: str}
    parsed_hints: Optional[Dict[int, str]] = None
    if page_hints:
        import json as _json
        try:
            raw = _json.loads(page_hints)
            parsed_hints = {int(k): v for k, v in raw.items()}
        except Exception:
            raise HTTPException(400, "page_hints must be valid JSON: {\"<page_idx>\": \"<hint>\"}")

    # Save uploaded file
    with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name

    try:
        # Generate case ID if not provided
        if not case_id:
            case_id = f"case_{int(time.time())}_{file.filename[:20]}"

        # Load PDF
        images = pdf_processor.load_pdf(tmp_path)

        # Process pages
        results = engine.process_document(images, case_id, page_hints=parsed_hints)
        
        # Build response
        decisions = []
        for i, result in enumerate(results):
            decisions.append(PageDecision(
                page_id=i,
                should_use_color=result.should_use_color,
                final_score=result.final_score,
                confidence=result.confidence,
                is_override=result.is_override,
                override_reason=result.override_reason,
                reasoning=result.explanation.to_dict(),
                processing_time_ms=0.0  # Would need to track individually
            ))
        
        # Get audit summary
        summary = engine.get_audit_report(case_id)
        
        return AnalysisResponse(
            case_id=case_id,
            total_pages=len(images),
            decisions=decisions,
            summary=summary
        )
        
    finally:
        # Cleanup
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/analyze/page")
async def analyze_single_page(
    file: UploadFile = File(...),
    page_id: int = 0,
    case_id: Optional[str] = None
):
    """
    Analyze a single image (JPEG, PNG) to determine if it needs color.
    """
    from .pdf_processor import load_image
    
    # Save uploaded file
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        tmp_path = tmp.name
    
    try:
        # Load image
        image = load_image(tmp_path)
        
        # Initialize case if needed
        if not case_id:
            case_id = f"case_{int(time.time())}"
        
        if engine._current_context is None or engine._current_context.case_id != case_id:
            engine.initialize_case(case_id, file.filename)
        
        # Process page
        result = engine.process_page(page_id, image)
        
        return {
            "page_id": page_id,
            "case_id": case_id,
            "should_use_color": result.should_use_color,
            "final_score": result.final_score,
            "confidence": result.confidence,
            "is_override": result.is_override,
            "override_reason": result.override_reason,
            "reasoning": result.explanation.to_dict()
        }
        
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.get("/cases/{case_id}/report")
async def get_case_report(case_id: str):
    """
    Get audit report for a processed case.
    """
    report = engine.get_audit_report(case_id)
    
    if "error" in report:
        raise HTTPException(404, report["error"])
    
    return report


@app.post("/reset")
async def reset_engine():
    """
    Reset engine for new case per specification section 16.
    """
    engine.reset_case()
    return {"status": "reset", "message": "Engine reset for new case"}


@app.get("/health")
async def health_check():
    """Check system health and available modules."""
    return {
        "status": "healthy",
        "modules": {
            "pdf_processor": pdf_processor.is_available(),
            "ocr": engine.ocr_layer.is_available(),
            "legal_bert": engine.semantic_analyzer._model is not None
        }
    }


@app.get("/config")
async def get_config():
    """Get current engine configuration."""
    config = engine.config
    return {
        "weights": {
            "visual": config.wv,
            "semantic": config.ws,
            "role": config.wr,
            "reference": config.wc,
            "attention": config.wa
        },
        "thresholds": {
            "saturation": config.saturation_threshold,
            "min_color_density": config.min_color_density,
            "rich_color_density": config.rich_color_density,
            "moderate_entropy": config.moderate_entropy,
            "rich_entropy": config.rich_entropy
        },
        "performance": {
            "target_ms_per_page": config.target_ms_per_page,
            "max_workers": config.max_workers
        }
    }
