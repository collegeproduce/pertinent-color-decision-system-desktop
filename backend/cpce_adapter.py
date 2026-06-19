"""
CPCE Adapter

Bridges the CPCE v19 engine output to this product's existing per-page schema
(PageRecord/DocumentResult from models.py), so app.py / csv_exporter.py /
override flow keep working unchanged.

Design rules:
- The cpce/ folder is treated as immutable. No edits there.
- One CPCEEngine instance is created at module import and reused across uploads
  (model load is paid once, at backend boot).
- `decision_zone == "review_required"` collapses to BW per product decision —
  real-world testing showed most review-required pages are actually BW.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

# Ensure stdout can carry the engine's Unicode print banners on Windows.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

from cpce.engine import CPCEEngine, DecisionResult
from cpce.pdf_processor import PDFProcessor
from models import PageRecord, DocumentResult, PrintMode, MetadataSource
from highlight_rescue import find_highlighted_pages
from large_doc import should_use_fast_path, process_large_pdf


class _SourceShim:
    """Stand-in for MetadataSource so format_page_data().metadata_source.value works."""
    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = value


# Pick a writable log directory regardless of where the bundle was launched
# from. When Electron spawns us with cwd=Program Files\..., the engine's
# default 'logs' directory would land in a read-only folder — give it an
# absolute path under the user's temp area instead.
_LOG_DIR = Path(tempfile.gettempdir()) / "PertinentColorApp" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)


# Module-level singletons — built once, reused for every upload.
print("Building CPCE engine (one-time)...")
_engine = CPCEEngine(log_dir=str(_LOG_DIR))
_pdf_processor = PDFProcessor()
print(f"CPCE engine ready (logs: {_LOG_DIR})")


def _map_decision(dr: DecisionResult, page_id: int) -> PageRecord:
    """Convert one engine DecisionResult to a PageRecord."""
    # Collapse review_required → BW (product decision based on real-world testing).
    is_color = bool(dr.should_use_color) and dr.decision_zone != "review_required"

    pr = PageRecord(page_id=page_id)
    pr.final_print_mode = PrintMode.COLOR if is_color else PrintMode.BW

    # bw_guaranteed feeds the efficiency stat. The new engine "decides" every page,
    # so any final BW counts as a saved page from the user's perspective.
    pr.bw_guaranteed = (not is_color)
    pr.color_candidate = is_color

    # Source: the engine's dominant_signal is the closest analog to old MetadataSource.
    src_label = dr.dominant_signal or "engine"
    pr.metadata_source = _SourceShim(src_label)

    # Reason: prefer the engine's human-readable dominant_factor_text. Fall back
    # through reasoning, override_reason, then a composed default.
    reason = (
        dr.dominant_factor_text
        or dr.reasoning
        or dr.override_reason
        or f"page_role={dr.page_role}, score={dr.final_score:.2f}, conf={dr.confidence:.2f}"
    )

    if dr.decision_zone == "review_required":
        reason = f"REVIEW (collapsed to BW): {reason}"
    if dr.is_override and dr.override_reason:
        reason = f"OVERRIDE: {dr.override_reason} | {reason}"

    pr.decision_details = reason
    return pr


def process_pdf(pdf_path: str, doc_id: Optional[str] = None) -> DocumentResult:
    """
    Run the CPCE v19 engine on a PDF and return a DocumentResult shaped exactly
    like the old optimized pipeline produced.

    The doc_id arg is accepted for caller compatibility but the engine maintains
    its own case identity internally.
    """
    if not _pdf_processor.is_available():
        raise RuntimeError(
            "PDF processing unavailable — PyMuPDF and OpenCV must be installed."
        )

    case_id = doc_id or f"case_{Path(pdf_path).stem}"

    # Large-document fast path. For docs >= LARGE_DOC_THRESHOLD pages, cheaply
    # triage out monochrome pages (no rasterizing) and only feed the uncertain
    # ones to the engine — avoiding the whole-document-into-RAM OOM and the long
    # runtime on huge records. Smaller docs fall through to the unchanged path.
    page_count = _pdf_processor.get_page_count(pdf_path)
    if should_use_fast_path(page_count):
        pages = process_large_pdf(
            pdf_path,
            _engine,
            pdf_processor_factory=lambda dpi: PDFProcessor(dpi=dpi),
            map_decision=_map_decision,
            case_id=case_id,
        )
        return DocumentResult(total_pages=page_count, pages=pages)

    # Stage 0: rasterise pages once, pass list around.
    images = _pdf_processor.load_pdf(pdf_path)
    if not images:
        raise RuntimeError(f"PDF has no pages: {pdf_path}")

    _engine.initialize_case(case_id, str(pdf_path))

    # Stages 1-11: the full CPCE pipeline.
    decisions: List[DecisionResult] = _engine.process_document(
        images,
        pdf_path=str(pdf_path),
        page_hints=None,
    )

    # Map back to the product's existing schema.
    pages: List[PageRecord] = [
        _map_decision(dr, page_id=i + 1) for i, dr in enumerate(decisions)
    ]

    # Highlight rescue (orchestration-layer safety net).
    # The engine's pixel-based yellow gate (saturation >= 100) misses pale
    # "flattened" highlights — e.g. Westlaw/Lexis exports rendered as a vector
    # fill of RGB(255,255,173), saturation ~81 — and sends those pages to B&W,
    # losing the highlight on print. Re-inspect ONLY the B&W pages using PDF
    # structure (annotations + colored vector fills under text). This can only
    # flip B&W -> COLOR, so it cannot regress any page already decided COLOR.
    bw_indices = [
        i for i, pr in enumerate(pages) if pr.final_print_mode == PrintMode.BW
    ]
    if bw_indices:
        try:
            rescued = find_highlighted_pages(str(pdf_path), bw_indices)
        except Exception as exc:  # never let the safety net break a real result
            print(f"  Highlight rescue skipped (error): {exc}")
            rescued = {}
        for i, reason in rescued.items():
            pr = pages[i]
            pr.final_print_mode = PrintMode.COLOR
            pr.bw_guaranteed = False
            pr.color_candidate = True
            pr.metadata_source = _SourceShim("highlight_rescue")
            pr.decision_details = (
                f"HIGHLIGHT RESCUE ({reason}) — pale/flattened highlight the "
                f"pixel engine missed | prior: {pr.decision_details}"
            )
        if rescued:
            print(f"  Highlight rescue: {len(rescued)} page(s) flipped BW->COLOR")

    return DocumentResult(total_pages=len(images), pages=pages)


class CPCEPipelineAdapter:
    """
    Drop-in replacement for OptimizedColorPrintingPipeline.

    Matches the old signature: process_document(filepath, progress_callback=None,
    doc_id=None) -> DocumentResult. The progress_callback is invoked at the
    start and end so the existing app.py reporting hook still fires; per-stage
    streaming arrives in Phase 3.
    """

    def __init__(self, max_workers: int = 8):
        # max_workers is honoured later in Phase 2's parallel orchestration.
        self.max_workers = max_workers

    def process_document(
        self,
        filepath: str,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
        doc_id: Optional[str] = None,
    ) -> DocumentResult:
        if progress_callback:
            try:
                progress_callback(0, 0, "Loading PDF...")
            except Exception:
                pass

        result = process_pdf(filepath, doc_id=doc_id)

        if progress_callback:
            try:
                progress_callback(result.total_pages, result.total_pages, "Done")
            except Exception:
                pass

        return result
