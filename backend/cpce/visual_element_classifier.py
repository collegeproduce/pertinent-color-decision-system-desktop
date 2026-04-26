"""
CPCE v14 - Visual Evidence Differentiation Engine
Multi-signal classification of visual regions in legal documents.

Classifies each detected region into one of:
  evidence_photo, chart, graph, exhibit_label, signature,
  highlight, logo, stamp, decorative, ambiguous

Uses 5 independent signals (never a single feature):
  shape_score    (0.25) — contours, Hough lines, geometry
  texture_score  (0.20) — variance, entropy, edge density
  color_score    (0.15) — saturation profile, color clustering, gradients
  spatial_score  (0.15) — position, size, alignment on page
  text_ctx_score (0.25) — OCR keyword patterns near the region

v14 changes:
  - source_hint parameter: validated chart/photo regions from ChartValidator
    bypass ambiguity and get a strong class-specific bias before final threshold
  - Area-weighted visual_evidence_score: large evidence regions dominate small logos
  - Tightened logo guard: size_ratio > 0.03 (was 0.05) demotes logo to near-zero
  - Final class = max(combined_score) with threshold:
      >= ASSIGN_THRESHOLD (0.75)    → assign class
      >= AMBIGUOUS_THRESHOLD (0.50) → "ambiguous"
      else                          → "decorative"
"""
import math
import cv2
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

# ─────────────────────────────────────────────────────────────
# Class registry and constants
# ─────────────────────────────────────────────────────────────

ELEMENT_CLASSES: List[str] = [
    'evidence_photo', 'chart', 'graph',
    'exhibit_label', 'signature', 'highlight',
    'logo', 'stamp', 'decorative',
]

# Pertinence impact per class — used by the pertinence engine to weight regions
ELEMENT_PERTINENCE: Dict[str, float] = {
    'evidence_photo': 1.00,
    'chart':          0.90,
    'graph':          0.85,
    'exhibit_label':  0.95,
    'signature':      0.65,
    'stamp':          0.70,
    'highlight':      0.50,
    'logo':           0.10,
    'decorative':     0.00,
    'ambiguous':      0.30,
    'noise':          0.00,
}

# OCR keyword patterns per class
_TEXT_PATTERNS: Dict[str, List[str]] = {
    'chart':          ['chart', 'figure', 'bar graph', 'pie chart', 'trend', 'axis', 'fig.'],
    'graph':          ['graph', 'plot', 'scatter', 'line graph', 'figure', 'diagram'],
    'evidence_photo': ['photograph', 'photo', 'image', 'picture', 'shown above', 'exhibit photo'],
    'exhibit_label':  ['exhibit', 'attachment', 'appendix', 'see above', 'refer to', 'attached hereto'],
    'signature':      ['signed', 'signature', 'sign by', 'notary', 'witness', '/s/', 'counsel'],
    'stamp':          ['received', 'filed', 'certified', 'official', 'seal', 'approved'],
    'highlight':      ['note', 'important', 'see highlighted', 'marked'],
    'logo':           ['logo', 'firm', 'associates', 'llp', 'corp', 'inc', 'letterhead'],
}

ASSIGN_THRESHOLD:    float = 0.75
AMBIGUOUS_THRESHOLD: float = 0.50


# ─────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────

@dataclass
class RegionClassification:
    """Full classification result for one detected visual region."""
    element_type:     str   # final assigned class
    confidence:       float  # max combined score
    pertinence_impact: float  # from ELEMENT_PERTINENCE

    # Per-signal score breakdowns (each is a class→score dict)
    shape_score:    Dict[str, float] = field(default_factory=dict)
    texture_score:  Dict[str, float] = field(default_factory=dict)
    color_score:    Dict[str, float] = field(default_factory=dict)
    spatial_score:  Dict[str, float] = field(default_factory=dict)
    text_ctx_score: Dict[str, float] = field(default_factory=dict)
    combined_score: Dict[str, float] = field(default_factory=dict)

    # Step-by-step reasoning trace
    trace: List[str] = field(default_factory=list)

    # Region geometry on page
    bbox: Tuple[int, int, int, int] = (0, 0, 0, 0)  # (x, y, w, h)


# ─────────────────────────────────────────────────────────────
# Classifier
# ─────────────────────────────────────────────────────────────

class VisualElementClassifier:
    """
    Classifies visual regions using 5-signal multi-factor reasoning.

    Hard rules (never violated):
      - NEVER classify highlight as chart (checked at combination stage)
      - NEVER classify logo as evidence (size + texture guard)
      - NEVER classify decorative noise as evidence
      - ALWAYS require at least 2 concordant signals for high-confidence assignment
    """

    def classify_region(
        self,
        region_img: np.ndarray,
        bbox: Tuple[int, int, int, int],
        page_img: Optional[np.ndarray],
        ocr_text: str = "",
        source_hint: str = "",
    ) -> RegionClassification:
        """
        Classify a single visual region.

        Args:
            region_img:   cropped BGR image of the region
            bbox:         (x, y, w, h) position on the full page
            page_img:     full page BGR image (for spatial context)
            ocr_text:     full-page OCR text (no per-region positions needed)
            source_hint:  v14 — 'chart' or 'photo' when the upstream detector
                          (ChartValidator / photo detector) already validated the
                          region. Injects a strong class bias before final threshold.

        Returns:
            RegionClassification with type, confidence, signal breakdown, trace.
        """
        trace: List[str] = []
        x, y, w, h = bbox
        page_h = page_img.shape[0] if page_img is not None else 1
        page_w = page_img.shape[1] if page_img is not None else 1

        # Guard: empty region
        if region_img is None or region_img.size == 0 or w < 5 or h < 5:
            return RegionClassification(
                element_type='noise', confidence=0.0, pertinence_impact=0.0,
                trace=["Region is empty or too small — noise"], bbox=bbox,
            )

        # ── Signal 1: Shape Analysis ─────────────────────────────────
        shape_sc = self._score_shape(region_img, w, h)
        top_shape = max(shape_sc, key=shape_sc.get)
        trace.append(
            f"Step 1 (Shape): leading='{top_shape}' ({shape_sc[top_shape]:.2f}) — "
            f"contours / Hough lines / geometry"
        )

        # ── Signal 2: Texture Analysis ───────────────────────────────
        texture_sc = self._score_texture(region_img)
        top_tex = max(texture_sc, key=texture_sc.get)
        trace.append(
            f"Step 2 (Texture): leading='{top_tex}' ({texture_sc[top_tex]:.2f}) — "
            f"local variance / entropy / edge density"
        )

        # ── Signal 3: Color Distribution ─────────────────────────────
        color_sc = self._score_color(region_img)
        top_color = max(color_sc, key=color_sc.get)
        trace.append(
            f"Step 3 (Color): leading='{top_color}' ({color_sc[top_color]:.2f}) — "
            f"saturation / clustering / highlight hues"
        )

        # ── Signal 4: Spatial Position ───────────────────────────────
        spatial_sc = self._score_spatial(bbox, page_w, page_h)
        top_spat = max(spatial_sc, key=spatial_sc.get)
        cx_norm = (x + w / 2) / max(page_w, 1)
        cy_norm = (y + h / 2) / max(page_h, 1)
        size_ratio = (w * h) / max(page_w * page_h, 1)
        trace.append(
            f"Step 4 (Spatial): leading='{top_spat}' ({spatial_sc[top_spat]:.2f}) — "
            f"center=({cx_norm:.2f},{cy_norm:.2f}) size_ratio={size_ratio:.4f}"
        )

        # ── Signal 5: Text Context ───────────────────────────────────
        text_sc = self._score_text_context(ocr_text)
        top_text = max(text_sc, key=text_sc.get)
        trace.append(
            f"Step 5 (Text context): leading='{top_text}' ({text_sc[top_text]:.2f}) — "
            f"OCR keyword matching"
        )

        # ── Combined Score (weighted sum) ────────────────────────────
        combined: Dict[str, float] = {}
        for cls in ELEMENT_CLASSES:
            combined[cls] = round(
                0.25 * shape_sc.get(cls, 0.0) +
                0.20 * texture_sc.get(cls, 0.0) +
                0.15 * color_sc.get(cls, 0.0) +
                0.15 * spatial_sc.get(cls, 0.0) +
                0.25 * text_sc.get(cls, 0.0),
                4,
            )

        # ── Hard distinction rules (prevent known confusions) ────────
        # NEVER: highlight > chart unless chart is dominant by shape AND texture
        if combined.get('highlight', 0) > combined.get('chart', 0):
            if shape_sc.get('chart', 0) > 0.4 and texture_sc.get('chart', 0) > 0.3:
                combined['highlight'] *= 0.5  # demote highlight when chart evidence is strong

        # v14: tightened logo guard — logos are small (< 3% of page, down from 5%)
        if size_ratio > 0.03:
            combined['logo'] = min(combined.get('logo', 0), 0.15)

        # ── Top-zone hard cap ────────────────────────────────────────
        # Any region whose center sits in the top 20% of the page AND occupies
        # less than 8% of the page is in the header/letterhead zone.
        # Exhibit stamps, company logos, colored title bars, and letterhead images
        # all live here — they are NEVER evidence photos or charts.
        in_top_zone = cy_norm < 0.20 and size_ratio < 0.08
        if in_top_zone:
            combined['evidence_photo'] = min(combined.get('evidence_photo', 0), 0.20)
            combined['chart']          = min(combined.get('chart', 0),          0.22)
            combined['graph']          = min(combined.get('graph', 0),           0.22)
            combined['logo']           = max(combined.get('logo', 0),            0.60)
            combined['exhibit_label']  = max(combined.get('exhibit_label', 0),  0.55)
            trace.append(
                f"Top-zone guard: cy_norm={cy_norm:.2f} size_ratio={size_ratio:.4f} — "
                "evidence_photo/chart capped at 0.20/0.22; logo boosted to ≥0.60"
            )

        # ── v14: Source-hint bias — trust upstream validators ───────
        # Chart regions that passed ChartValidator are real charts.
        # Photo regions that passed photo detector are real photos.
        # Without this, the 5-signal classifier often re-labels them as decorative
        # because chart/photo crops can look like flat colored rectangles at crop level.
        #
        # Position guard: the upstream detectors now reject top-zone regions, so a
        # source_hint arriving here should already be in the body.  The guard below
        # is a belt-and-suspenders check — if somehow a top-zone region still carries
        # a hint, treat it as a logo/exhibit label rather than applying the high floor.
        if source_hint == 'chart':
            if not in_top_zone:
                combined['chart'] = max(combined.get('chart', 0), 0.82)
                combined['graph'] = max(combined.get('graph', 0), 0.65)
                combined['decorative'] = min(combined.get('decorative', 0), 0.15)
                trace.append(
                    "Source hint 'chart': upstream ChartValidator confirmed — "
                    "chart score floored at 0.82, decorative capped at 0.15"
                )
            else:
                trace.append(
                    "Source hint 'chart' ignored: region is in top-zone (logo/header area)"
                )
        elif source_hint == 'photo':
            if not in_top_zone:
                combined['evidence_photo'] = max(combined.get('evidence_photo', 0), 0.78)
                combined['decorative'] = min(combined.get('decorative', 0), 0.15)
                trace.append(
                    "Source hint 'photo': upstream photo detector confirmed — "
                    "evidence_photo score floored at 0.78"
                )
            else:
                combined['logo'] = max(combined.get('logo', 0), 0.65)
                trace.append(
                    "Source hint 'photo' ignored: region is in top-zone — "
                    "reclassified as logo/exhibit label"
                )

        # ── Assignment ───────────────────────────────────────────────
        best_class = max(combined, key=combined.get)
        best_score = combined[best_class]

        if best_score >= ASSIGN_THRESHOLD:
            element_type = best_class
        elif best_score >= AMBIGUOUS_THRESHOLD:
            element_type = 'ambiguous'
        else:
            element_type = 'decorative'

        pertinence_impact = ELEMENT_PERTINENCE.get(element_type, 0.0)

        trace.append(
            f"Step 6 (Combined): best='{best_class}' {best_score:.3f} → "
            f"assigned '{element_type}' (pertinence_impact={pertinence_impact:.2f})"
        )
        trace.append(
            f"Conclusion: {element_type.upper()} — confidence {best_score:.3f} — "
            f"{'contributes to case' if pertinence_impact > 0.3 else 'low evidentiary value'}"
        )

        return RegionClassification(
            element_type=element_type,
            confidence=round(best_score, 4),
            pertinence_impact=pertinence_impact,
            shape_score=shape_sc,
            texture_score=texture_sc,
            color_score=color_sc,
            spatial_score=spatial_sc,
            text_ctx_score=text_sc,
            combined_score=combined,
            trace=trace,
            bbox=bbox,
        )

    def classify_page_regions(
        self,
        photo_regions: List[Dict],
        chart_regions: List[Dict],
        img: np.ndarray,
        ocr_text: str = "",
    ) -> Tuple[List[RegionClassification], float]:
        """
        Classify all detected regions on a page and compute aggregate visual evidence score.

        v14: Area-weighted formula:
            visual_evidence_score = SUM(impact × confidence × region_area) / page_area

        This ensures large evidence regions (charts, photos) dominate over small logos.
        Small decorative elements that happen to appear in many counts do not inflate the score.

        source_hint is passed per-region so chart/photo validators are respected.

        Returns:
            (list_of_classifications, visual_evidence_score in [0, 1])
        """
        classifications: List[RegionClassification] = []
        page_h = img.shape[0] if img is not None else 1
        page_w = img.shape[1] if img is not None else 1
        page_area = max(page_h * page_w, 1)

        def _crop(bbox):
            if img is None:
                return np.zeros((8, 8, 3), dtype=np.uint8)
            x, y, w, h = bbox
            x, y = max(0, x), max(0, y)
            w = min(w, page_w - x)
            h = min(h, page_h - y)
            if w <= 0 or h <= 0:
                return np.zeros((8, 8, 3), dtype=np.uint8)
            return img[y:y + h, x:x + w]

        # Photo regions — upstream detector already validated these
        for ri in photo_regions:
            bbox = ri.get('bbox', (0, 0, 0, 0))
            clf = self.classify_region(_crop(bbox), bbox, img, ocr_text, source_hint='photo')
            classifications.append(clf)

        # Chart regions — ChartValidator already confirmed these
        for ri in chart_regions:
            bbox = ri.get('bbox', (0, 0, 0, 0))
            clf = self.classify_region(_crop(bbox), bbox, img, ocr_text, source_hint='chart')
            classifications.append(clf)

        if not classifications:
            return [], 0.0

        # v15: Non-linear type-split formula — mirrors human legal perception.
        #
        # Why linear averaging fails:
        #   6 small charts (1% each) × 0.9 impact × 0.82 conf / page_area = ~0.04
        #   → system sees 0.04, treats as decorative, calls B/W.  WRONG.
        #
        # Human paralegal reasoning:
        #   "Six charts on one page = this page IS the financial evidence."
        #
        # Formula:
        #   chart_score * 1.5   (charts are primary legal evidence)
        #   photo_score * 1.2   (photos strong but slightly secondary)
        #   text_score  * 0.8   (highlights, stamps, signatures are supporting)
        #   + count boost: +0.20 for 3+ chart regions, +0.10 for 2
        #
        _CHART_TYPES = {'chart', 'graph'}
        _PHOTO_TYPES = {'evidence_photo'}
        _TEXT_TYPES  = {'highlight', 'exhibit_label', 'signature', 'stamp'}

        def _contrib(c: 'RegionClassification') -> float:
            return c.pertinence_impact * c.confidence * (c.bbox[2] * c.bbox[3])

        chart_area = sum(_contrib(c) for c in classifications if c.element_type in _CHART_TYPES)
        photo_area = sum(_contrib(c) for c in classifications if c.element_type in _PHOTO_TYPES)
        text_area  = sum(_contrib(c) for c in classifications if c.element_type in _TEXT_TYPES)

        raw_ve = (chart_area * 1.5 + photo_area * 1.2 + text_area * 0.8) / page_area

        # Count-based boost: even small charts signal legal relevance when numerous
        chart_count = sum(1 for c in classifications if c.element_type in _CHART_TYPES)
        if chart_count >= 3:
            raw_ve += 0.20
        elif chart_count >= 2:
            raw_ve += 0.10

        visual_evidence_score = min(1.0, raw_ve)
        return classifications, round(visual_evidence_score, 4)

    # ──────────────────────────────────────────────────────────────
    # Signal 1: Shape Analysis
    # ──────────────────────────────────────────────────────────────
    def _score_shape(self, region: np.ndarray, rw: int, rh: int) -> Dict[str, float]:
        """
        Scores from shape geometry:
          - Hough lines → horizontal/vertical line count (charts/graphs have structured grids)
          - Contour count + circularity (signatures are curvilinear; stamps are circular)
          - Aspect ratio (highlights are wide & thin; exhibit labels are taller)
          - Edge density (photos have complex organic edges; highlights have few)
        """
        scores = {cls: 0.0 for cls in ELEMENT_CLASSES}

        try:
            gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if len(region.shape) == 3 else region.copy()
            edges = cv2.Canny(gray, 50, 150)
            edge_density = float(np.mean(edges > 0))

            # Hough lines
            lines = cv2.HoughLinesP(
                edges, 1, np.pi / 180, threshold=15,
                minLineLength=max(min(rw, rh) * 0.15, 5),
                maxLineGap=8
            )
            h_lines = v_lines = 0
            if lines is not None:
                for ln in lines:
                    x1, y1, x2, y2 = ln[0]
                    angle = abs(math.degrees(math.atan2(y2 - y1, x2 - x1)))
                    if angle < 12 or angle > 168:
                        h_lines += 1
                    elif 78 < angle < 102:
                        v_lines += 1

            structured = (h_lines >= 2 and v_lines >= 1) or (h_lines >= 1 and v_lines >= 2)

            # Contour analysis
            contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contour_count = len(contours)
            max_circularity = 0.0
            if contours:
                areas = [cv2.contourArea(c) for c in contours]
                largest = contours[int(np.argmax(areas))]
                area = float(cv2.contourArea(largest))
                perim = float(cv2.arcLength(largest, True))
                if perim > 1:
                    max_circularity = min(1.0, 4 * math.pi * area / (perim * perim))

            aspect = rw / max(rh, 1)
            region_area = rw * rh

            # Chart / graph: structured axis grid
            if structured:
                grid_strength = min(1.0, (h_lines + v_lines) / 8)
                scores['chart'] = min(1.0, 0.45 + grid_strength * 0.55)
                scores['graph'] = min(1.0, 0.40 + grid_strength * 0.45)

            # Highlight: very wide & thin band, few horizontal lines
            if aspect > 4.0 and rh < 25 and edge_density < 0.25:
                scores['highlight'] = min(1.0, 0.5 + (aspect - 4) * 0.04)
            elif h_lines >= 3 and v_lines == 0 and aspect > 3.0:
                scores['highlight'] = 0.55

            # Signature: irregular, curvilinear, not structured, few contours
            if not structured and max_circularity < 0.25 and 2 <= contour_count <= 20:
                scores['signature'] = min(1.0, 0.30 + (1 - max_circularity) * 0.40)

            # Stamp: near-circular dominant contour
            if max_circularity > 0.55 and 0.7 < aspect < 1.4 and region_area < 40000:
                scores['stamp'] = min(1.0, 0.40 + max_circularity * 0.50)

            # Logo: compact icon OR wide horizontal header strip
            # Compact (square-ish) icon: aspect 0.6–1.6, small area
            if 0.6 < aspect < 1.6 and region_area < 18000 and edge_density > 0.08:
                scores['logo'] = 0.45
            # Wide letterhead / title banner: very wide and flat
            elif aspect > 4.0 and region_area < 80000 and edge_density > 0.04:
                scores['logo'] = 0.55  # wide horizontal bands are headers, not photos

            # Evidence photo: large region, many organic contours, not structured
            # Must NOT be a wide flat strip (aspect > 3.0 = banner or header element)
            if not structured and contour_count > 30 and region_area > 35000 and aspect < 3.0:
                scores['evidence_photo'] = min(1.0, 0.40 + edge_density * 0.60)

            # Exhibit label: tall narrow text block
            if aspect < 0.6 and contour_count > 15:
                scores['exhibit_label'] = 0.35

            # Decorative: very low edge density
            if edge_density < 0.04:
                scores['decorative'] = min(1.0, 0.40 + (0.04 - edge_density) * 15)

        except Exception:
            scores['decorative'] = 0.5

        return scores

    # ──────────────────────────────────────────────────────────────
    # Signal 2: Texture Analysis
    # ──────────────────────────────────────────────────────────────
    def _score_texture(self, region: np.ndarray) -> Dict[str, float]:
        """
        Scores from texture properties:
          - Global variance → photos are high; highlights low
          - Entropy → charts are moderate; photos high; highlights low
          - Local variance (block-wise) → natural content has high local variation
          - Edge density → graphs have sharp edges; photos have distributed edges
        """
        scores = {cls: 0.0 for cls in ELEMENT_CLASSES}

        try:
            gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY) if len(region.shape) == 3 else region.copy()
            gf = gray.astype(float)

            variance = float(np.var(gf))

            hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
            hist /= hist.sum() + 1e-8
            entropy = float(-np.sum(hist * np.log2(hist + 1e-10)))

            # Block-wise local variance (4×4 grid)
            gh, gw = gf.shape
            bh, bw = max(1, gh // 4), max(1, gw // 4)
            local_vars = [
                float(np.var(gf[r:r + bh, c:c + bw]))
                for r in range(0, gh - bh + 1, bh)
                for c in range(0, gw - bw + 1, bw)
            ]
            mean_lv = float(np.mean(local_vars)) if local_vars else 0.0

            edges = cv2.Canny(gray, 50, 150)
            edge_density = float(np.mean(edges > 0))

            # Evidence photo: high variance + high entropy + high local variation
            if variance > 800 and entropy > 4.5:
                scores['evidence_photo'] = min(1.0, (variance / 3000) * 0.6 + (entropy / 8) * 0.4)

            # Chart/graph: moderate entropy, low-moderate variance, structured edges
            if variance < 600 and 3.0 < entropy < 6.5 and edge_density > 0.04:
                chart_t = min(1.0, 0.40 + edge_density * 2.5)
                scores['chart'] = chart_t
                scores['graph'] = min(1.0, chart_t * 0.90)

            # Highlight: very uniform — very low variance, low entropy
            if variance < 150 and entropy < 3.0:
                scores['highlight'] = min(1.0, 1.0 - variance / 150)

            # Signature: moderate variance concentrated in a few strokes
            if 150 < variance < 1500 and mean_lv < 400 and edge_density > 0.04:
                scores['signature'] = min(1.0, 0.30 + edge_density * 2.0)

            # Logo: clean compact lines, low variance
            if variance < 500 and edge_density > 0.07 and entropy < 5.0:
                scores['logo'] = min(1.0, 0.25 + edge_density * 2.0)

            # Stamp: moderate variance (ink on paper)
            if 200 < variance < 2500 and edge_density > 0.05:
                scores['stamp'] = 0.30

            # Decorative: very low everything
            if variance < 80 and edge_density < 0.04:
                scores['decorative'] = min(1.0, 0.50 + (80 - variance) / 80 * 0.40)

        except Exception:
            scores['decorative'] = 0.4

        return scores

    # ──────────────────────────────────────────────────────────────
    # Signal 3: Color Distribution
    # ──────────────────────────────────────────────────────────────
    def _score_color(self, region: np.ndarray) -> Dict[str, float]:
        """
        Scores from color properties:
          - Natural color variation (high sat std, gradient) → evidence_photo
          - Few discrete uniform colors → chart/logo
          - Highlight hues (yellow/green) → highlight
          - Red/blue at high saturation → stamp
          - Near grayscale (low saturation) → signature / decorative
        """
        scores = {cls: 0.0 for cls in ELEMENT_CLASSES}

        if len(region.shape) < 3:
            # Grayscale region
            scores['decorative'] = 0.5
            scores['signature'] = 0.3
            return scores

        try:
            hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
            sat = hsv[:, :, 1].astype(float)
            hue = hsv[:, :, 0].astype(float)
            val = hsv[:, :, 2].astype(float)

            mean_sat = float(np.mean(sat))
            std_sat  = float(np.std(sat))
            mean_val = float(np.mean(val))

            # Distinct hue clusters (colored pixels only)
            sat_mask = sat > 30
            n_colored = int(np.sum(sat_mask))
            n_distinct = 0
            if n_colored > 80:
                hist, _ = np.histogram(hue[sat_mask], bins=8, range=(0, 180))
                n_distinct = int(np.sum(hist > n_colored * 0.05))

            # Gradient strength (smooth gradients → natural photo)
            rh, rw = val.shape
            gradient_mag = 0.0
            if rh > 8 and rw > 8:
                dx = cv2.Sobel(val.astype(np.uint8), cv2.CV_64F, 1, 0, ksize=3)
                dy = cv2.Sobel(val.astype(np.uint8), cv2.CV_64F, 0, 1, ksize=3)
                gradient_mag = float(np.mean(np.sqrt(dx ** 2 + dy ** 2)))

            # Highlight color masks (yellow H=18-42, green H=38-85, pink H=140-180)
            hl_yellow = (hue >= 18) & (hue <= 42) & (sat > 80)
            hl_green  = (hue >= 38) & (hue <= 85) & (sat > 80)
            hl_pink   = (hue >= 140) & (hue <= 180) & (sat > 80)
            highlight_ratio = float(np.mean(hl_yellow | hl_green | hl_pink))

            # Stamp color masks (red H=0-12 or 168-180, blue H=100-135 at high sat)
            st_red  = (((hue >= 0) & (hue <= 12)) | ((hue >= 168) & (hue <= 180))) & (sat > 110)
            st_blue = (hue >= 100) & (hue <= 135) & (sat > 110)
            stamp_ratio = float(np.mean(st_red | st_blue))

            # Evidence photo: natural variation
            if mean_sat > 35 and std_sat > 25 and gradient_mag > 4:
                scores['evidence_photo'] = min(1.0, mean_sat / 120 + std_sat / 100)

            # Chart: a few discrete saturated colors (bars, pie slices)
            if 2 <= n_distinct <= 7 and mean_sat > 25:
                scores['chart'] = min(1.0, 0.25 + n_distinct * 0.09)
                scores['graph'] = min(1.0, 0.20 + n_distinct * 0.07)

            # Highlight
            if highlight_ratio > 0.04:
                scores['highlight'] = min(1.0, highlight_ratio * 6)

            # Stamp
            if stamp_ratio > 0.015:
                scores['stamp'] = min(1.0, stamp_ratio * 12)

            # Signature: near grayscale, dark ink on white
            if mean_sat < 25 and mean_val < 210:
                scores['signature'] = min(1.0, 0.40 + (25 - mean_sat) / 50)

            # Logo: high saturation, compact color set
            if mean_sat > 55 and n_distinct <= 4 and n_distinct > 0:
                scores['logo'] = min(1.0, 0.25 + mean_sat / 200)

            # Decorative: near white / near grayscale, low saturation
            if mean_sat < 12 and mean_val > 200:
                scores['decorative'] = min(1.0, 0.50 + (200 - mean_val) / 200)

        except Exception:
            scores['decorative'] = 0.4

        return scores

    # ──────────────────────────────────────────────────────────────
    # Signal 4: Spatial Position
    # ──────────────────────────────────────────────────────────────
    def _score_spatial(
        self, bbox: Tuple[int, int, int, int], page_w: int, page_h: int
    ) -> Dict[str, float]:
        """
        Scores from page position:
          - Center body → evidence_photo / chart
          - Bottom corners → signature
          - Top edge, small → logo / exhibit label
          - Thin horizontal band → highlight
          - Bottom body → stamp
        """
        scores = {cls: 0.0 for cls in ELEMENT_CLASSES}
        x, y, w, h = bbox
        page_w = max(page_w, 1)
        page_h = max(page_h, 1)

        cx_norm = (x + w / 2) / page_w
        cy_norm = (y + h / 2) / page_h
        size_ratio = (w * h) / (page_w * page_h)
        aspect = w / max(h, 1)

        in_top    = cy_norm < 0.15
        in_body   = 0.15 <= cy_norm <= 0.85
        in_bottom = cy_norm > 0.85
        in_left   = cx_norm < 0.18
        in_right  = cx_norm > 0.82
        in_center = 0.15 <= cx_norm <= 0.85

        # Evidence photo: large, centered in body
        # Raised minimum size_ratio from 0.04 to 0.08 — a real evidence photo
        # must be substantial (occupying at least 8% of the page).
        # Small colored boxes (exhibit stamps, thumbnails) won't qualify.
        if size_ratio > 0.08 and in_body and in_center:
            scores['evidence_photo'] = min(1.0, 0.40 + size_ratio * 4)

        # Chart/graph: medium-large, body, wider than tall
        # Raised minimum size_ratio from 0.025 to 0.05 — real charts are substantial.
        # Tiny colored boxes and icon-sized images won't qualify.
        if size_ratio > 0.05 and in_body and aspect > 0.7:
            scores['chart'] = min(1.0, 0.35 + size_ratio * 3)
            scores['graph'] = min(1.0, 0.30 + size_ratio * 3)

        # Highlight: thin wide band
        if aspect > 3.5 and size_ratio < 0.008:
            scores['highlight'] = min(1.0, 0.50 + (aspect - 3.5) * 0.03)

        # Exhibit label: top area, any width
        if in_top or cy_norm < 0.28:
            scores['exhibit_label'] = 0.50

        # Signature: bottom third, especially bottom corners
        if cy_norm > 0.70:
            scores['signature'] = 0.45
            if in_left or in_right:
                scores['signature'] = 0.65

        # Logo: small-medium element in the top zone (letterhead, exhibit stamp, company logo).
        # Covers both compact corner logos and wider header banners.
        # Also catches exhibit stamp boxes in the top-left or top-right corners
        # (which appear in the top 25% of the page and are smaller than 8% of page area).
        if in_top and size_ratio < 0.08:
            if in_left or in_right:
                scores['logo'] = 0.75  # corner position strongly suggests logo/stamp label
            else:
                scores['logo'] = 0.60  # centered header item
        elif cy_norm < 0.25 and size_ratio < 0.04 and (in_left or in_right):
            # Top-quarter corner items that just missed in_top — exhibit stamps often here
            scores['logo'] = 0.70

        # Stamp: small-medium, often in body or bottom
        if 0.003 < size_ratio < 0.06:
            scores['stamp'] = 0.28
            if in_bottom or cy_norm > 0.70:
                scores['stamp'] = 0.48

        # Decorative: very small anywhere
        if size_ratio < 0.002:
            scores['decorative'] = 0.70

        return scores

    # ──────────────────────────────────────────────────────────────
    # Signal 5: Text Context
    # ──────────────────────────────────────────────────────────────
    def _score_text_context(self, text: str) -> Dict[str, float]:
        """
        Scores from OCR keyword patterns.
        Each pattern hit adds 0.20 to the class score (capped at 1.0).
        Near-zero by default so pages without relevant text stay neutral.
        """
        scores = {cls: 0.0 for cls in ELEMENT_CLASSES}
        if not text:
            return scores
        tl = text.lower()
        for cls, patterns in _TEXT_PATTERNS.items():
            hits = sum(1 for p in patterns if p in tl)
            if hits:
                scores[cls] = min(1.0, hits * 0.20)
        return scores
