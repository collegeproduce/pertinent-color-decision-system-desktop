"""
Large-document fast path (orchestration layer — cpce/ stays untouched).

WHY
---
The CPCE engine is whole-document and all-in-RAM by design: it rasterizes every
page at 150 DPI into memory at once, then runs document-level TF-IDF, clustering
and cross-page evidence-linking over the entire page set. For a 2000-page record
that is ~12 GB of page images before analysis even starts — which is exactly why
huge PDFs OOM / take forever.

WHAT
----
For documents at or above LARGE_DOC_THRESHOLD pages ONLY, this adds a cheap
CASCADE TRIAGE using PDF structure (PyMuPDF) — no rasterizing:

  * A page that provably carries no color — has a text layer, no embedded raster
    images, no colored vector fills/strokes, no markup annotations — is decided
    B&W immediately and never rasterized or sent to the engine. (A page with no
    color content can never *need* color, so this matches what the engine would
    decide anyway.)
  * Every other page (images, colored vectors, annotations, or no text at all)
    is "uncertain" and IS sent to the full engine, unchanged.

The uncertain pages are handed to the engine as a coherent temporary sub-PDF, so
the engine's text<->image<->page alignment (it reads text via load_page(i)) is
preserved. A memory guard lowers the rasterization DPI only if the uncertain set
would still blow the RAM budget, so image-heavy giant docs degrade gracefully
instead of crashing.

SAFETY
------
Documents below the threshold do NOT enter this module — they run exactly as
before, byte-for-byte. Within a large doc, the only pages skipped are ones with
zero detectable color, and the triage is deliberately conservative (any image or
any colored mark => full engine).
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None

from highlight_rescue import find_highlighted_pages

# Only documents with at least this many pages take the fast path.
LARGE_DOC_THRESHOLD = 300

# Rasterization DPI the engine is tuned for, and the floor we will not go below
# (its pixel-area thresholds for stamps/charts assume ~150 DPI).
ENGINE_DPI = 150
MIN_DPI = 100

# Rough RAM budget for the rasterized uncertain-page set (bytes). Above this the
# DPI is reduced to fit. ~3 GB keeps headroom on a typical 16 GB machine.
RASTER_BUDGET_BYTES = 3 * 1024 * 1024 * 1024

# Reuse the same "is this fill a real colored mark, not gray/black/white" logic
# the highlight rescue uses, so triage and rescue agree.
_GRAY_TOL = 0.06
_NEAR_WHITE = 0.93
_NEAR_BLACK = 0.15
_MARKUP_ANNOT_TYPES = {8, 9, 10, 11}  # Highlight, Underline, StrikeOut, Squiggly


def _is_grayish(rgb) -> bool:
    r, g, b = rgb
    return (max(r, g, b) - min(r, g, b)) <= _GRAY_TOL


def _has_colored_vector(page) -> bool:
    try:
        for d in page.get_drawings():
            for key in ("fill", "color"):  # fill = filled shape, color = stroke
                c = d.get(key)
                if not c or _is_grayish(c):
                    continue
                if min(c) > _NEAR_WHITE or max(c) < _NEAR_BLACK:
                    continue
                return True
    except Exception:
        # If drawings can't be read, be conservative and treat as uncertain.
        return True
    return False


def _has_markup_annot(page) -> bool:
    try:
        annots = page.annots()
        if annots:
            for a in annots:
                if a.type[0] in _MARKUP_ANNOT_TYPES:
                    return True
    except Exception:
        return True
    return False


def _page_needs_engine(page) -> bool:
    """
    Conservative triage: return True (=> full engine) unless the page provably
    has no color. Only a text-bearing page with no images, no colored vectors and
    no markup annotations — or a completely blank page — is skipped as B&W.
    """
    try:
        has_text = bool(page.get_text("text").strip())
    except Exception:
        return True  # unreadable text → let the engine handle it

    try:
        has_images = len(page.get_images(full=False)) > 0
    except Exception:
        has_images = True

    if has_images:
        return True
    if _has_colored_vector(page):
        return True
    if _has_markup_annot(page):
        return True

    # No images, no colored marks. If there is text it's monochrome text → B&W.
    # If there's no text at all it's a blank page → B&W. Either way: skip engine.
    return False


def _choose_dpi(num_pages: int, page_area_pt: float) -> int:
    """Pick a rasterization DPI so num_pages fit the RAM budget; never below MIN_DPI."""
    if num_pages <= 0:
        return ENGINE_DPI
    # bytes per page ≈ (w_pt/72 * dpi) * (h_pt/72 * dpi) * 3 channels
    px_per_pt2_at = lambda dpi: (dpi / 72.0) ** 2
    bytes_at_engine = num_pages * page_area_pt * px_per_pt2_at(ENGINE_DPI) * 3
    if bytes_at_engine <= RASTER_BUDGET_BYTES:
        return ENGINE_DPI
    scale = math.sqrt(RASTER_BUDGET_BYTES / max(bytes_at_engine, 1.0))
    return max(MIN_DPI, int(ENGINE_DPI * scale))


def should_use_fast_path(page_count: int) -> bool:
    return fitz is not None and page_count >= LARGE_DOC_THRESHOLD


def process_large_pdf(
    pdf_path: str,
    engine,
    pdf_processor_factory: Callable[[int], object],
    map_decision: Callable[[object, int], object],
    case_id: str,
) -> List[object]:
    """
    Run the cascade fast path. Returns a list of PageRecord (already mapped via
    `map_decision`), one per original page, in order.

    pdf_processor_factory(dpi) -> a PDFProcessor instance at that DPI.
    map_decision(decision_result, page_id) -> PageRecord (the adapter's mapper).
    """
    from models import PageRecord, PrintMode

    doc = fitz.open(pdf_path)
    n = len(doc)

    # ── Triage (cheap, no rasterizing) ────────────────────────────────────
    candidate_idx: List[int] = []
    page_area_pt = 612.0 * 792.0  # default Letter; refined from first page
    try:
        r = doc[0].rect
        page_area_pt = float(r.width * r.height)
    except Exception:
        pass

    for i in range(n):
        if _page_needs_engine(doc[i]):
            candidate_idx.append(i)

    skipped = n - len(candidate_idx)
    print(f"  [large-doc] {n} pages → {len(candidate_idx)} need engine, "
          f"{skipped} decided B&W by triage")

    # Pre-build B&W records for every page; candidates get overwritten below.
    bw_reason = "FAST-PATH triage: no images, colored vectors, or annotations — monochrome page"
    pages: List[PageRecord] = []
    for i in range(n):
        pr = PageRecord(page_id=i + 1)
        pr.final_print_mode = PrintMode.BW
        pr.bw_guaranteed = True
        pr.color_candidate = False
        pr.metadata_source = _SourceShim("fast_path_triage")
        pr.decision_details = bw_reason
        pages.append(pr)

    if not candidate_idx:
        doc.close()
        return pages

    # ── Build a coherent sub-PDF of just the candidate pages ──────────────
    tmp_dir = Path(tempfile.gettempdir()) / "PertinentColorApp" / "subpdf"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    sub_path = tmp_dir / f"sub_{case_id}.pdf"
    sub = fitz.open()
    sub.insert_pdf(doc, from_page=0, to_page=0)  # placeholder to init; removed next
    sub.delete_page(0)
    for i in candidate_idx:
        sub.insert_pdf(doc, from_page=i, to_page=i)
    sub.save(str(sub_path))
    sub.close()
    doc.close()

    # ── Rasterize candidates (with memory guard) and run the engine ───────
    dpi = _choose_dpi(len(candidate_idx), page_area_pt)
    if dpi < ENGINE_DPI:
        print(f"  [large-doc] memory guard: rasterizing candidates at {dpi} DPI "
              f"(engine default {ENGINE_DPI})")
    processor = pdf_processor_factory(dpi)
    images = processor.load_pdf(str(sub_path))

    engine.initialize_case(case_id, str(sub_path))
    decisions = engine.process_document(images, pdf_path=str(sub_path), page_hints=None)

    # ── Map engine decisions back to original page positions ──────────────
    engine_bw_original: List[int] = []
    for local_i, orig_i in enumerate(candidate_idx):
        if local_i < len(decisions):
            pr = map_decision(decisions[local_i], page_id=orig_i + 1)
            pages[orig_i] = pr
            if pr.final_print_mode == PrintMode.BW:
                engine_bw_original.append(orig_i)

    # ── Highlight rescue on the engine's B&W candidates (original indices) ─
    try:
        rescued = find_highlighted_pages(pdf_path, engine_bw_original)
    except Exception as exc:
        print(f"  [large-doc] highlight rescue skipped: {exc}")
        rescued = {}
    for orig_i, reason in rescued.items():
        pr = pages[orig_i]
        pr.final_print_mode = PrintMode.COLOR
        pr.bw_guaranteed = False
        pr.color_candidate = True
        pr.metadata_source = _SourceShim("highlight_rescue")
        pr.decision_details = (
            f"HIGHLIGHT RESCUE ({reason}) — pale/flattened highlight the pixel "
            f"engine missed | prior: {pr.decision_details}"
        )

    # ── Cleanup temp sub-PDF ──────────────────────────────────────────────
    try:
        sub_path.unlink()
    except Exception:
        pass

    return pages


class _SourceShim:
    """Mirror of the adapter's shim so PageRecord.metadata_source.value works."""
    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = value
