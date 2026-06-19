"""
Highlight rescue — orchestration-layer safety net for missed highlights.

WHY THIS EXISTS
---------------
The CPCE engine's highlight detection is pixel-based. Its yellow gate
(visual_analyzer.HIGHLIGHT_YELLOW) requires HSV saturation >= 100. "Flattened"
highlights exported by legal-research platforms (Westlaw / Lexis / court ECF)
are often rendered as a pale vector fill — e.g. RGB(255, 255, 173), whose
saturation is only ~81. That falls UNDER the engine's gate, so the engine sees
zero yellow, calls the page B&W, and the highlight is lost on print.

Measured on the real file 4C_-_736.0807_Delegation_by_trustee.pdf:
    annots = []                          (not a PDF annotation)
    colored vector fills = 13 @ (1.0, 1.0, 0.68)   <- the highlight
    engine_yellow_density (S>=100) = 0.00000        <- missed
    median yellow saturation = 81                   <- just below the 100 gate

WHAT IT DOES
------------
Re-inspects ONLY the pages the engine already decided B&W, using the PDF's
STRUCTURE rather than pixels — so it is robust to pale colors that defeat HSV
thresholds:

    Layer 1  annotations   text-markup highlight annots (Highlight / Underline /
                           StrikeOut / Squiggly).
    Layer 2  vector fills   non-gray fill shapes that sit underneath real text
                           words (a colored rectangle behind words == highlight).

SAFETY
------
It can ONLY flip B&W -> COLOR. It never flips COLOR -> B&W, so it cannot regress
any page the engine already prints in color (saturated Lexis gold highlights,
photos, stamps, charts all keep their existing decision). The cpce/ engine is
immutable; this lives in the product orchestration layer.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - packaged build always ships fitz
    fitz = None


# Text-markup annotation subtypes that mean "someone marked this text".
# PyMuPDF annotation type codes: Highlight=8, Underline=9, StrikeOut=10, Squiggly=11.
_MARKUP_ANNOT_TYPES = {8, 9, 10, 11}

# A fill counts as "colored" (highlight-like) only if it is clearly not a
# gray/black/white box (table shading, rules, paper). Tuned so pale yellow
# RGB(1.0, 1.0, 0.68) passes while gray shading / near-white / near-black fail.
_GRAY_TOLERANCE = 0.06   # max channel spread to still be considered gray
_NEAR_WHITE = 0.93       # fills brighter than this on every channel are paper
_NEAR_BLACK = 0.15       # fills darker than this on every channel are ink/black box

# Minimum number of distinct text words sitting on colored fill before we treat
# it as a genuine highlight (vs. a small colored glyph or icon).
_MIN_WORDS_ON_FILL = 3

# Minimum fill HEIGHT (PDF points) to count as a highlight. A real highlight is a
# block covering a line of text (~9pt tall for ~10pt body text). Hyperlink
# underlines and rule lines render as ~1pt-tall colored fills — they sit "under
# text" too, so without this gate they were wrongly rescued to COLOR. Measured:
# real highlights ≈ 9.1pt; hyperlink underlines ≈ 1.0pt — 4pt cleanly separates.
_MIN_FILL_HEIGHT_PT = 4.0


def _is_grayish(rgb: Tuple[float, float, float]) -> bool:
    r, g, b = rgb
    return (max(r, g, b) - min(r, g, b)) <= _GRAY_TOLERANCE


def _rects_overlap(a, b) -> bool:
    # a, b are (x0, y0, x1, y1)
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _page_has_highlight(page) -> Optional[str]:
    """Return a short reason string if the page carries a highlight, else None."""
    # ── Layer 1: text-markup annotations (cheap, definitive) ──────────────
    try:
        annots = page.annots()
        if annots:
            for a in annots:
                if a.type[0] in _MARKUP_ANNOT_TYPES:
                    return f"annotation:{a.type[1]}"
    except Exception:
        pass  # malformed annots — fall through to vector inspection

    # ── Layer 2: colored vector fills sitting under real text ─────────────
    try:
        words = page.get_text("words")  # list of (x0, y0, x1, y1, word, ...)
    except Exception:
        words = None
    if not words:
        return None

    colored_rects = []
    try:
        for d in page.get_drawings():
            fill = d.get("fill")
            if not fill or _is_grayish(fill):
                continue
            r, g, b = fill
            if min(r, g, b) > _NEAR_WHITE or max(r, g, b) < _NEAR_BLACK:
                continue  # paper-white or near-black box, not a highlight
            rect = d.get("rect")
            if rect is None:
                continue
            if (rect.y1 - rect.y0) < _MIN_FILL_HEIGHT_PT:
                continue  # thin line (hyperlink underline / rule), not a highlight block
            colored_rects.append((rect.x0, rect.y0, rect.x1, rect.y1))
    except Exception:
        return None

    if not colored_rects:
        return None

    covered = 0
    for w in words:
        wb = (w[0], w[1], w[2], w[3])
        if any(_rects_overlap(wb, fr) for fr in colored_rects):
            covered += 1
            if covered >= _MIN_WORDS_ON_FILL:
                # sample the first fill color for a readable reason
                fr = colored_rects[0]
                return f"vector_fill x{len(colored_rects)} under text ({covered}+ words)"
    return None


def find_highlighted_pages(
    pdf_path: str,
    page_indices: Iterable[int],
) -> Dict[int, str]:
    """
    Inspect the given 0-based page indices of `pdf_path` for highlights.

    Only the supplied indices are examined (callers pass the pages the engine
    decided B&W), so this stays cheap even on very large documents.

    Returns {page_index: reason} for pages that DO carry a highlight.
    """
    if fitz is None:
        return {}

    wanted = sorted({int(i) for i in page_indices})
    if not wanted:
        return {}

    found: Dict[int, str] = {}
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return {}

    try:
        n = len(doc)
        for idx in wanted:
            if idx < 0 or idx >= n:
                continue
            try:
                reason = _page_has_highlight(doc[idx])
            except Exception:
                reason = None
            if reason:
                found[idx] = reason
    finally:
        doc.close()

    return found
