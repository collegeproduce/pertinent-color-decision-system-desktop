"""
CPCE v5 - Visual Analyzer
OpenCV-based color analysis per specification section 1.
Enhanced to detect evidence photos, stamps, exhibit labels, charts, graphs, highlights.
"""
import cv2
import numpy as np
from scipy.stats import entropy as shannon_entropy
from typing import Tuple, List, Dict
from .models import VisualFeatures, PageRole, CPCEConfig
from .chart_validator import ChartValidator, ChartValidationResult


class VisualAnalyzer:
    """
    Analyzes visual properties using OpenCV.
    Implements color meaningfulness detection per CPCE v5 specification.
    Enhanced for legal documents: detects photos, stamps, exhibits, charts, highlights.
    """
    
    # Legal document color thresholds
    HIGHLIGHT_YELLOW = {
        'hsv_lower': (20, 100, 150),  # Yellow highlight
        'hsv_upper': (40, 255, 255),
        'name': 'yellow_highlight'
    }
    HIGHLIGHT_GREEN = {
        'hsv_lower': (40, 100, 150),  # Green highlight
        'hsv_upper': (80, 255, 255),
        'name': 'green_highlight'
    }
    HIGHLIGHT_PINK = {
        'hsv_lower': (140, 100, 150),  # Pink/red highlight
        'hsv_upper': (180, 255, 255),
        'name': 'pink_highlight'
    }
    
    # Stamp colors (typically red, blue, or black)
    STAMP_RED = {
        'hsv_lower': (0, 150, 100),
        'hsv_upper': (10, 255, 255),
        'name': 'red_stamp'
    }
    STAMP_BLUE = {
        'hsv_lower': (100, 150, 100),
        'hsv_upper': (130, 255, 255),
        'name': 'blue_stamp'
    }
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
        self.highlights = [self.HIGHLIGHT_YELLOW, self.HIGHLIGHT_GREEN, self.HIGHLIGHT_PINK]
        self.stamps = [self.STAMP_RED, self.STAMP_BLUE]
        self.chart_validator = ChartValidator()
    
    def analyze(self, img: np.ndarray, page_role: PageRole = PageRole.UNKNOWN) -> VisualFeatures:
        """
        Perform full visual analysis on an image.
        Detects photos, stamps, highlights, charts, and exhibit labels.
        """
        features = VisualFeatures()
        
        # Check for valid image
        if img is None or img.size == 0:
            features.meaningfulness_reason = "invalid_image"
            return features
        
        # Ensure uint8 for OpenCV
        if img.dtype != np.uint8:
            img = np.clip(img, 0, 255).astype(np.uint8)
        
        # Convert to float64 for numerical stability
        img_float = img.astype(np.float64)
        
        # Calculate color density
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)   # shared — reused by all detectors
        sat = hsv[:, :, 1]
        features.color_density = float(np.mean(sat > self.config.saturation_threshold))
        
        # Calculate highlight density (yellow, green, pink markers)
        highlight_mask = self._detect_highlights(hsv)
        features.highlight_density = float(np.mean(highlight_mask))

        # v22: highlighted TEXT density — highlight pixels that overlap dark text strokes.
        # A highlight with no text underneath is just a color blob (decorative or noise).
        # Only highlights with dark ink (text characters) inside them are attorney annotations.
        if features.highlight_density > 0.0:
            # Dark pixels = text strokes (< 80 in gray for typical scanned/printed text)
            text_mask = (gray < 80).astype(np.uint8)
            # Dilate to catch text ON TOP of highlight (text sits above the ink layer)
            kernel = np.ones((3, 3), np.uint8)
            text_near_highlight = cv2.dilate(text_mask, kernel, iterations=2) > 0
            overlap_mask = highlight_mask & text_near_highlight

            # Global density (fraction of page that is highlight+text)
            features.highlighted_text_density = float(np.mean(overlap_mask))

            # Strictness check: what fraction of the highlight area has text beneath it?
            # A loose yellow wash over a blank margin has low text-fraction.
            # A legal annotation over actual words has high text-fraction (≥ 0.25).
            hl_pixels = int(np.sum(highlight_mask))
            if hl_pixels > 0:
                text_fraction = float(np.sum(overlap_mask)) / hl_pixels
                # If less than 25% of the highlight area has text, treat as decorative.
                if text_fraction < 0.25:
                    features.highlighted_text_density = 0.0

            # Yellow-only variant — yellow is the attorney annotation standard.
            yellow_mask = cv2.inRange(
                hsv,
                self.HIGHLIGHT_YELLOW['hsv_lower'],
                self.HIGHLIGHT_YELLOW['hsv_upper'],
            ) > 0
            yellow_hl_pixels = int(np.sum(yellow_mask))
            if yellow_hl_pixels > 0:
                yellow_overlap = yellow_mask & text_near_highlight
                yellow_text_fraction = float(np.sum(yellow_overlap)) / yellow_hl_pixels
                if yellow_text_fraction >= 0.25:
                    features.yellow_highlighted_text_density = float(np.mean(yellow_overlap))
                else:
                    features.yellow_highlighted_text_density = 0.0
            else:
                features.yellow_highlighted_text_density = 0.0
        else:
            features.highlighted_text_density = 0.0
            features.yellow_highlighted_text_density = 0.0
        
        # Detect stamps and seals
        stamp_mask = self._detect_stamps(hsv)
        features.stamp_density = float(np.mean(stamp_mask))
        
        # Detect evidence photos (areas with natural color patterns)
        photo_regions = self._detect_photo_regions(img, hsv)
        features.photo_regions = len(photo_regions)
        features.photo_region_data = photo_regions   # v10: store for classifier

        # Detect charts and graphs (geometric patterns with color)
        chart_regions, rejected_charts = self._detect_charts_and_graphs(img, hsv)
        features.chart_regions = len(chart_regions)
        features.chart_region_data = chart_regions   # v10: store for classifier

        # v19: Non-color visual intelligence — DISABLED pending threshold calibration.
        # Re-enable by removing the early assignments below.
        gray_regions, sig_regions, bw_stamp_regs = [], [], []
        features.grayscale_regions = 0
        features.signature_regions = 0
        features.bw_stamp_regions = 0

        # v20: Colored annotation text — red/orange attorney markings in body text
        features.colored_annotation_density = self._detect_colored_annotation_text(img, hsv)

        # Calculate entropy on grayscale  (gray already computed above)
        hist = cv2.calcHist([gray], [0], None, [256], [0, 256])
        hist = hist.flatten() / hist.sum()
        features.entropy = float(shannon_entropy(hist + self.config.epsilon))
        
        # Calculate spatial distribution
        features.spatial_distribution = self._calculate_spatial_distribution(hsv)
        
        # Calculate header vs body color
        features.header_fraction, features.body_fraction = self._calculate_header_body_split(hsv)
        
        # Determine meaningfulness based on ALL factors
        features.is_color_meaningful = self._determine_meaningfulness(features, page_role)
        features.meaningfulness_reason = self._get_meaningfulness_reason(features)
        
        # Detected elements summary
        features.detected_elements = {
            'photos': int(features.photo_regions),
            'charts': int(features.chart_regions),
            'highlights': bool(np.sum(highlight_mask) > 1000),
            'stamps': bool(np.sum(stamp_mask) > 500),
            'color_density': float(features.color_density),
            'has_evidence_color': bool(features.photo_regions > 0 or features.chart_regions > 0),
            # v19: non-color elements
            'grayscale_images': int(features.grayscale_regions),
            'signatures': int(features.signature_regions),
            'bw_stamps': int(features.bw_stamp_regions),
            'has_noncolor_visual': bool(
                features.grayscale_regions > 0 or
                features.signature_regions > 0 or
                features.bw_stamp_regions > 0
            ),
            # v20: colored annotation text
            'colored_annotations': float(features.colored_annotation_density),
        }
        
        return features
    
    def _detect_highlights(self, hsv: np.ndarray) -> np.ndarray:
        """Detect yellow, green, pink highlights."""
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for highlight in self.highlights:
            color_mask = cv2.inRange(hsv, highlight['hsv_lower'], highlight['hsv_upper'])
            mask = cv2.bitwise_or(mask, color_mask)
        return mask > 0
    
    def _detect_stamps(self, hsv: np.ndarray) -> np.ndarray:
        """
        Detect red and blue stamps/seals — shape-aware, not just color-aware.

        A real stamp or seal is a spatially COMPACT, DENSE cluster of colored ink:
          - Circular or rectangular (not scattered across lines like text)
          - Minimum connected-component area: 1,500 px²
          - Fill ratio within bounding box: ≥ 0.35  (dense ink block, not sparse text)
          - Aspect ratio: 0.25 – 4.0  (not an ultra-thin line or wide banner)
          - Maximum size: ≤ 12% of page area (stamps are not full-page color blocks)

        Red-colored text annotations produce many SMALL, THIN, SCATTERED components
        that all fail the minimum area and fill-ratio gates.

        Returns a mask where ONLY qualifying stamp-like regions are filled.
        This mask is used by `_determine_meaningfulness` via np.mean(),
        so it must only be non-zero where real stamp-shaped blobs exist.
        """
        page_h, page_w = hsv.shape[:2]
        page_area = max(page_h * page_w, 1)

        # Minimum blob area: eliminates text characters and punctuation marks
        MIN_STAMP_BLOB  = 1500    # px²
        # Fill ratio: compact stamp ink vs scattered text strokes
        MIN_FILL_RATIO  = 0.35
        # Aspect ratio bounds: stamps are roughly compact (not long thin lines)
        MIN_ASPECT      = 0.25
        MAX_ASPECT      = 4.0
        # Maximum fraction of page: a real stamp is never a full-page color block
        MAX_SIZE_RATIO  = 0.12

        raw_mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for stamp in self.stamps:
            color_mask = cv2.inRange(hsv, stamp['hsv_lower'], stamp['hsv_upper'])
            raw_mask = cv2.bitwise_or(raw_mask, color_mask)

        if not np.any(raw_mask):
            return raw_mask.astype(bool)

        # Close small gaps within a single stamp blob, but do NOT merge distant elements
        close_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        closed = cv2.morphologyEx(raw_mask, cv2.MORPH_CLOSE, close_kernel)

        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        stamp_mask = np.zeros(hsv.shape[:2], dtype=np.uint8)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < MIN_STAMP_BLOB:
                continue  # Too small — text character, noise, or punctuation

            x, y, w, h = cv2.boundingRect(cnt)
            aspect = float(w) / max(h, 1)
            if not (MIN_ASPECT <= aspect <= MAX_ASPECT):
                continue  # Too elongated — a colored line or narrow text column

            fill_ratio = area / max(float(w * h), 1.0)
            if fill_ratio < MIN_FILL_RATIO:
                continue  # Sparse — scattered text strokes across multiple lines

            size_ratio = float(w * h) / page_area
            if size_ratio > MAX_SIZE_RATIO:
                continue  # Too large — colored background, not a stamp

            # This blob qualifies as a stamp-like region: fill it in the output mask
            cv2.drawContours(stamp_mask, [cnt], -1, 255, thickness=cv2.FILLED)

        return stamp_mask.astype(bool)
    
    def _detect_colored_annotation_text(self, img: np.ndarray, hsv: np.ndarray) -> float:
        """
        Detect colored body text annotations (red, orange, dark-pink attorney markings).

        These are NOT stamps (no compact blob required), NOT highlights (different hue),
        and NOT photos (no filled region).  They are colored INK in body text used by
        attorneys to annotate, mark up, or emphasize legal content.

        The detector distinguishes annotation text from two common false-positive sources:
          • Vertical margin lines  → narrow column coverage (< 20% of page width)
          • Compact color blobs    → low row-spread, caught by stamp detector instead

        Returns:
            colored_annotation_density [0, 1] — 0 means no qualifying annotation text.
            Values above ~0.05 indicate visually meaningful colored annotation content.
        """
        page_h, page_w = img.shape[:2]
        hue = hsv[:, :, 0].astype(np.int32)
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]

        # ── Annotation color ranges (high saturation = genuine ink, not faded) ──
        # Red:    H = 0–15  or  165–180  in OpenCV (sat ≥ 120)
        # Orange: H = 15–30             (sat ≥ 130)
        red_mask    = ((hue <= 15) | (hue >= 165)) & (sat > 120) & (val > 40) & (val < 235)
        orange_mask = ((hue > 15)  &  (hue <= 30)) & (sat > 130) & (val > 50) & (val < 235)
        annotation_mask = (red_mask | orange_mask).astype(np.uint8)

        if not np.any(annotation_mask):
            return 0.0

        # ── Exclude header zone (top 15%): logos, exhibit stamps, letterheads ──
        header_rows = int(page_h * 0.15)
        annotation_mask[:header_rows, :] = 0

        covered = int(np.sum(annotation_mask))
        if covered < 150:
            return 0.0  # Too few pixels to be body text

        body_area  = max((page_h - header_rows) * page_w, 1)
        coverage   = float(covered) / body_area

        if coverage < 0.0002:
            return 0.0  # Below noise floor

        body_mask = annotation_mask[header_rows:, :]

        # ── Horizontal distribution: text spans most of the line width ──────────
        # A vertical margin line fills only 1–5% of the page columns.
        # Body text lines span 40–90% of page width.
        cols_with_color = int(np.sum(np.any(body_mask, axis=0)))
        col_coverage    = cols_with_color / max(page_w, 1)
        if col_coverage < 0.20:
            return 0.0  # Narrow vertical band — margin line, not body text

        # ── Vertical distribution: must span multiple rows (multiple text lines) ─
        rows_with_color = int(np.sum(np.any(body_mask, axis=1)))
        if rows_with_color < 6:
            return 0.0  # Too few rows — single character / punctuation

        # ── Score: scale coverage to [0, 1], cap at 0.80 ──────────────────────
        # Typical annotated paragraph: coverage ≈ 0.005–0.015 → score 0.25–0.75
        score = min(0.80, coverage * 60.0)
        return round(score, 4)

    def _detect_photo_regions(self, img: np.ndarray, hsv: np.ndarray) -> List[Dict]:
        """Detect regions that look like photographs (natural color gradients)."""
        regions = []

        page_h, page_w = img.shape[:2]
        page_area = max(page_h * page_w, 1)

        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]

        # Saturation mask: include full saturation range (no upper cap — highly
        # saturated photos were previously excluded by sat < 200).
        photo_mask = (sat > 25) & (val > 40) & (val < 250)

        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (10, 10))
        photo_mask_closed = cv2.morphologyEx(
            photo_mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel
        )

        contours, _ = cv2.findContours(
            photo_mask_closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        def _check_contour(cnt, min_area, min_rel_size, fill_min, color_fill_min, std_min):
            area = cv2.contourArea(cnt)
            if area < min_area:
                return None
            x, y, w, h = cv2.boundingRect(cnt)
            aspect_ratio = float(w) / max(h, 1)
            # Expanded aspect ratio: 0.2–4.0 covers portrait, landscape, panoramic photos
            if not (0.2 < aspect_ratio < 4.0):
                return None
            fill_ratio = area / max(float(w * h), 1.0)
            if fill_ratio < fill_min:
                return None
            # Top-zone guard: skip stamp/logo area (top 15% of page)
            center_y = y + h / 2.0
            if center_y < page_h * 0.15:
                return None
            relative_size = (w * h) / page_area
            if relative_size < min_rel_size:
                return None
            # Texture variance: photos have tonal variation
            try:
                roi = img[y:y + h, x:x + w]
                if roi.size > 0:
                    gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                    if float(np.std(gray_roi)) < std_min:
                        return None
            except Exception:
                pass
            # Color coverage within bounding box
            try:
                mask_roi = photo_mask_closed[y:y + h, x:x + w]
                if float(np.mean(mask_roi > 0)) < color_fill_min:
                    return None
            except Exception:
                pass
            return {
                'type': 'photo',
                'bbox': (x, y, w, h),
                'area': area,
                'aspect_ratio': aspect_ratio,
                'fill_ratio': fill_ratio,
            }

        seen_bboxes = set()
        for cnt in contours:
            # Primary pass: larger photos (≥ 3% of page, strict guards)
            r = _check_contour(cnt,
                min_area=10000, min_rel_size=0.03,
                fill_min=0.35, color_fill_min=0.28, std_min=15.0)
            if r is None:
                # Secondary pass: smaller inset photos (≥ 1.5% of page, looser guards)
                r = _check_contour(cnt,
                    min_area=5000, min_rel_size=0.015,
                    fill_min=0.45, color_fill_min=0.40, std_min=20.0)
            if r is not None:
                key = r['bbox']
                if key not in seen_bboxes:
                    seen_bboxes.add(key)
                    regions.append(r)

        return regions
    
    def _detect_charts_and_graphs(self, img: np.ndarray, hsv: np.ndarray) -> Tuple[List[Dict], int]:
        """
        Detect and validate charts, graphs, and diagrams.
        Returns: (validated_regions, rejected_count)
        """
        candidate_regions = []
        
        # Charts often have distinct colored bars or lines
        sat = hsv[:, :, 1]
        
        # Find areas with distinct color blocks (chart bars)
        _, thresh = cv2.threshold(sat, 50, 255, cv2.THRESH_BINARY)
        
        # Dilate to connect nearby colored regions.
        # Reduced from (15,15)×2 to (10,10)×1 to prevent merging distant elements
        # (margin lines + headers) into spurious large L-shaped contours.
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 10))
        thresh = cv2.dilate(thresh, kernel, iterations=1)
        
        # Find contours
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        # v17: Minimum dimensions to reject icon bars (h≈47px) and thin decorative banners.
        # Real charts need enough vertical space for axes, labels, and data regions.
        MIN_CHART_HEIGHT = 120   # pixels — raised from 80: eliminates header banners, icon rows
        MIN_CHART_AREA   = 25000 # pixels² — raised from 15000: eliminates small colored boxes

        page_h_c, page_w_c = img.shape[:2]

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area > MIN_CHART_AREA:
                x, y, w, h = cv2.boundingRect(cnt)
                aspect = w / max(h, 1)
                # Charts are wider than tall, meet minimum height, and have a
                # reasonable aspect ratio (1.2–4.0).  Upper bound tightened from 5.0
                # to 4.0 — very wide flat strips are header banners, not charts.
                if not (w > h and w > 100 and h >= MIN_CHART_HEIGHT and 1.2 <= aspect <= 4.0):
                    continue

                # ── Top-zone guard: reject header/letterhead colored bands ──
                # Real charts are in the body of the document, not the page header.
                # Reject any candidate whose bounding box center sits in the top 20%.
                center_y = y + h / 2.0
                if center_y < page_h_c * 0.20:
                    continue  # header zone — colored title bars, letterheads, exhibit stamps

                # ── Minimum relative size guard ──
                # A real chart must occupy at least 2% of page area.
                if (w * h) / max(page_h_c * page_w_c, 1) < 0.02:
                    continue

                # ── Fill-ratio guard ──
                # Margin lines and borders produce L-shaped or thin contours
                # whose bounding rect is far larger than the actual colored area.
                # Real charts fill most of their bounding box (fill > 0.30).
                fill_ratio = area / max(float(w * h), 1.0)
                if fill_ratio < 0.30:
                    continue

                candidate_regions.append((x, y, w, h))
        
        # Validate each candidate region
        validated_regions = []
        rejected_count = 0
        
        for bbox in candidate_regions:
            validation = self.chart_validator.validate(img, bbox)
            
            if validation.is_valid_chart:
                validated_regions.append({
                    'type': 'chart_or_graph',
                    'bbox': bbox,
                    'confidence': validation.confidence_score,
                    'has_axes': validation.has_x_axis or validation.has_y_axis,
                    'has_data': validation.has_data_points
                })
            else:
                rejected_count += 1
                print(f"  Chart rejected: {validation.rejection_reason}")
        
        if rejected_count > 0:
            print(f"  Charts: {len(validated_regions)} valid, {rejected_count} rejected")
        
        return validated_regions, rejected_count
    
    # ── v19: Non-color visual intelligence ──────────────────────────────────

    def _detect_grayscale_images(self, img: np.ndarray, hsv: np.ndarray, gray: np.ndarray) -> List[Dict]:
        """
        Detect grayscale image blocks (B&W photos, scanned exhibits, medical images).

        Color pipeline misses these entirely because saturation ≈ 0.
        A grayscale image has mid-gray pixel values (not blank white, not pure black ink)
        in a large contiguous region with photographic tonal variation.

        Detection:
          1. Mask: sat < 25 AND 40 < val < 215  (mid-gray zone)
          2. Morphological close (large kernel) to merge image blocks
          3. Contour area > 18,000 px² (eliminates noise and small icons)
          4. Texture check: std of grayscale values in bbox > 12  (real image vs flat gray)
        """
        regions = []
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]

        # Mid-gray zone: not paper (too bright) and not ink strokes (too dark)
        gray_mask = ((sat < 25) & (val > 40) & (val < 215)).astype(np.uint8) * 255

        # Merge nearby pixels to form image blocks
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 30))
        gray_mask = cv2.morphologyEx(gray_mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(gray_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        gray_img = gray

        # Compute full-page edges once; slice per-contour in the loop (avoids repeated Canny calls)
        full_edges = cv2.Canny(gray_img, 30, 100)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 18000:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            aspect = float(w) / max(h, 1)
            if not (0.25 < aspect < 4.0):
                continue
            # Texture check: a real grayscale image has tonal variation
            roi = gray_img[y:y + h, x:x + w]
            if roi.size == 0:
                continue
            texture_std = float(np.std(roi))
            if texture_std < 12.0:
                continue  # Flat gray area (e.g. a shaded box) — not a photo

            # Rectangularity + edge-uniformity exclusion (v20):
            # Tables and shaded boxes have a rectangular fill (contour ≈ bounding rect)
            # combined with a regular/uniform edge pattern (grid lines).
            # Real photos have irregular contours AND spatially varied edge distributions.
            fill_ratio = area / max(float(w * h), 1.0)
            if fill_ratio > 0.82:
                # Highly rectangular — check edge distribution to distinguish photo vs table
                edges_roi = full_edges[y:y + h, x:x + w]
                # Low std of the edge map means edges are uniformly distributed (grid pattern)
                edge_map_std = float(np.std(edges_roi.astype(np.float32)))
                if edge_map_std < 40.0:
                    continue  # Rectangular + uniform edges = table or ruled box, not a photo

            regions.append({
                'type': 'grayscale_image',
                'bbox': (x, y, w, h),
                'area': area,
                'aspect_ratio': aspect,
                'texture_std': texture_std,
            })

        return regions

    def _detect_signature_patterns(self, img: np.ndarray, hsv: np.ndarray, gray: np.ndarray) -> List[Dict]:
        """
        Detect handwritten signature clusters (shape-based, color-agnostic).

        Signatures are invisible to the color pipeline (black ink, sat ≈ 0).
        Key properties vs body text:
          - Fewer, larger connected components (not dozens of character-sized blobs)
          - High curviness: perimeter >> bounding rectangle perimeter
          - Located in bottom 55% of the page
          - Horizontal orientation (width > height)

        Strategy:
          1. Work on bottom 55% of image
          2. Threshold to get ink (dark) pixels
          3. Find connected components; keep those in the "signature size" range
          4. Curviness ratio: perimeter / (2*(w+h)) > 1.4 (not a rectangular text block)
          5. Component density: 1–12 components per cluster (signatures are sparse ink)
        """
        regions = []
        h_img, w_img = img.shape[:2]
        top_start = int(h_img * 0.45)   # Focus on bottom 55%

        roi_img = img[top_start:, :]
        gray_roi = gray[top_start:, :]

        # Get ink pixels (dark on white paper)
        _, binary = cv2.threshold(gray_roi, 180, 255, cv2.THRESH_BINARY_INV)

        # Slight dilation to connect nearby pen strokes of the same glyph
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        binary = cv2.dilate(binary, k, iterations=1)

        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # Collect candidate signature strokes
        candidates = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 150 or area > 25000:
                continue   # Too small (noise/period) or too large (text block)
            x, y, w, h = cv2.boundingRect(cnt)
            aspect = float(w) / max(h, 1)
            if aspect < 0.5:
                continue   # Tall narrow strokes are not signatures
            perimeter = cv2.arcLength(cnt, True)
            rect_perimeter = 2.0 * (w + h)
            curviness = perimeter / max(rect_perimeter, 1.0)
            if curviness < 1.35:
                continue   # Rectangular blobs = text characters or boxes
            # Stroke width confidence (v20): handwritten ink is thin (2–14 px).
            # Estimate: stroke_width ≈ 2 × area / perimeter (skeleton approximation).
            # Thick shapes (> 15px) are filled blobs, stamps, or thick borders — not pen strokes.
            stroke_width = 2.0 * area / max(perimeter, 1.0)
            if stroke_width > 15.0:
                continue   # Too thick to be a handwritten pen stroke
            candidates.append({'cnt': cnt, 'x': x, 'y': y, 'w': w, 'h': h,
                                'area': area, 'curviness': curviness,
                                'stroke_width': stroke_width})

        if not candidates:
            return regions

        # Cluster candidates horizontally (signatures span a line)
        candidates.sort(key=lambda c: c['x'])
        used = [False] * len(candidates)

        for i, base in enumerate(candidates):
            if used[i]:
                continue
            cluster = [base]
            used[i] = True
            bx2 = base['x'] + base['w']
            by_center = base['y'] + base['h'] / 2.0

            for j, other in enumerate(candidates):
                if used[j]:
                    continue
                # Same horizontal band and horizontally adjacent to the growing cluster
                oy_center = other['y'] + other['h'] / 2.0
                h_overlap = abs(by_center - oy_center) < max(base['h'], other['h']) * 1.5
                x_close = other['x'] <= bx2 + int(w_img * 0.12)   # within 12% of page width
                if h_overlap and x_close and len(cluster) <= 12:
                    cluster.append(other)
                    used[j] = True
                    bx2 = max(bx2, other['x'] + other['w'])

            # A signature cluster has 1–8 strokes, not the dozens that text lines produce
            if not (1 <= len(cluster) <= 8):
                continue

            # Bounding box of the cluster
            cx = min(c['x'] for c in cluster)
            cy = min(c['y'] for c in cluster)
            cw = max(c['x'] + c['w'] for c in cluster) - cx
            ch = max(c['y'] + c['h'] for c in cluster) - cy
            cluster_aspect = float(cw) / max(ch, 1)
            if cluster_aspect < 1.0:
                continue   # Taller than wide — not a signature

            avg_curviness = float(np.mean([c['curviness'] for c in cluster]))
            avg_stroke_width = float(np.mean([c['stroke_width'] for c in cluster]))
            total_area = sum(c['area'] for c in cluster)

            # Cluster-level stroke width gate (v20): reject if average stroke is too thick.
            # A cluster of 1–3 strokes with avg_width > 12px is more likely a stamp border,
            # thick underline, or box rule than a handwritten signature.
            if avg_stroke_width > 12.0:
                continue

            regions.append({
                'type': 'signature',
                'bbox': (cx, top_start + cy, cw, ch),   # coords relative to full image
                'stroke_count': len(cluster),
                'avg_curviness': avg_curviness,
                'avg_stroke_width': avg_stroke_width,
                'total_ink_area': total_area,
                'cluster_aspect': cluster_aspect,
            })

        return regions

    def _detect_bw_stamps(self, img: np.ndarray, hsv: np.ndarray, gray: np.ndarray) -> List[Dict]:
        """
        Detect B&W notary seals, court stamps, and official circular impressions.

        These are invisible to the color stamp detector (STAMP_RED/STAMP_BLUE)
        because they use black or dark ink.

        Key properties:
          - High circularity: 4π·area/perimeter² > 0.35  (circular/oval shape)
          - Moderate size: 1,000–80,000 px² (postage-stamp to large seal)
          - Hollow ring or partially filled disk (stamp border with text inside)
          - Low eccentricity of fitted ellipse (roundish, not elongated)

        Strategy:
          1. Grayscale + adaptive threshold to isolate dark shapes
          2. Canny edges → Hough circles (robust to partial impressions)
          3. Also check contour circularity for filled/semi-filled seals
        """
        regions = []
        h_img, w_img = gray.shape

        # ── Method 1: Hough circle transform (catches clean circular borders)
        # Blur first to reduce noise from text inside the seal
        blurred = cv2.GaussianBlur(gray, (9, 9), 2)
        circles = cv2.HoughCircles(
            blurred,
            cv2.HOUGH_GRADIENT,
            dp=1.2,
            minDist=40,
            param1=60,
            param2=35,
            minRadius=20,
            maxRadius=min(h_img, w_img) // 4,
        )

        seen_centers = []

        if circles is not None:
            circles = np.round(circles[0]).astype(int)
            for cx, cy, r in circles:
                # Verify it's actually a dark circle (not a light artifact)
                mask = np.zeros(gray.shape, dtype=np.uint8)
                cv2.circle(mask, (cx, cy), r, 255, 3)   # Ring only
                ring_pixels = gray[mask > 0]
                if len(ring_pixels) == 0:
                    continue
                mean_ring_val = float(np.mean(ring_pixels))
                if mean_ring_val > 160:
                    continue   # Ring is light — not an ink stamp border
                regions.append({
                    'type': 'bw_stamp',
                    'method': 'hough_circle',
                    'bbox': (cx - r, cy - r, 2 * r, 2 * r),
                    'radius': int(r),
                    'center': (int(cx), int(cy)),
                    'ring_darkness': mean_ring_val,
                })
                seen_centers.append((cx, cy, r))

        # ── Method 2: Contour circularity (catches oval seals, rectangular stamps)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        # Remove thin ink strokes (text characters) — keep only substantial shapes
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)

        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 1000 or area > 80000:
                continue
            perimeter = cv2.arcLength(cnt, True)
            if perimeter < 1:
                continue
            circularity = 4.0 * np.pi * area / (perimeter ** 2)
            if circularity < 0.35:
                continue   # Too non-circular (a rectangle, line, or text blob)
            x, y, w, h = cv2.boundingRect(cnt)
            aspect = float(w) / max(h, 1)
            if not (0.5 < aspect < 2.0):
                continue   # Too elongated to be a seal

            # Don't double-count what Hough already found
            cx_c, cy_c = x + w // 2, y + h // 2
            duplicate = any(
                abs(cx_c - sc[0]) < sc[2] and abs(cy_c - sc[1]) < sc[2]
                for sc in seen_centers
            )
            if duplicate:
                continue

            regions.append({
                'type': 'bw_stamp',
                'method': 'contour_circularity',
                'bbox': (x, y, w, h),
                'area': area,
                'circularity': float(circularity),
            })

        return regions

    # ── End v19 non-color detectors ────────────────────────────────────────

    def _calculate_spatial_distribution(self, hsv: np.ndarray) -> float:
        """Calculate how evenly color is distributed across the page."""
        sat = hsv[:, :, 1]
        colored_pixels = sat > self.config.saturation_threshold
        
        # Divide into 4 quadrants
        h, w = sat.shape
        mid_h, mid_w = h // 2, w // 2
        
        quadrants = [
            colored_pixels[:mid_h, :mid_w],
            colored_pixels[:mid_h, mid_w:],
            colored_pixels[mid_h:, :mid_w],
            colored_pixels[mid_h:, mid_w:]
        ]
        
        quadrant_densities = [np.mean(q) for q in quadrants]
        
        # Calculate variance (lower variance = more even distribution)
        variance = np.var(quadrant_densities)
        
        # Return normalized distribution score (0-1)
        return float(1.0 - min(variance * 4, 1.0))
    
    def _calculate_header_body_split(self, hsv: np.ndarray) -> Tuple[float, float]:
        """Calculate color in header vs body of the page."""
        sat = hsv[:, :, 1]
        h, w = sat.shape
        
        # Header is top 15%
        header_zone = int(h * 0.15)
        header_sat = sat[:header_zone, :]
        body_sat = sat[header_zone:, :]
        
        header_fraction = float(np.mean(header_sat > self.config.saturation_threshold))
        body_fraction = float(np.mean(body_sat > self.config.saturation_threshold))
        
        return header_fraction, body_fraction
    
    def _determine_meaningfulness(self, features: VisualFeatures, page_role: PageRole) -> bool:
        """
        Determine if color is meaningful based on ALL detected elements.
        
        OVERRIDE RULES (highest priority):
        1. Evidence photos detected → ALWAYS meaningful
        2. Charts/diagrams (≥2) → ALWAYS meaningful  
        3. Highlighted text → ALWAYS meaningful
        4. Stamps/seals → ALWAYS meaningful
        
        DENSITY RULES (applied if no override):
        5. Color density < 0.08 → NOT meaningful
        6. Rich body color (density > 0.15) → meaningful
        7. Decorative header only → NOT meaningful
        """
        # --- OVERRIDES (bypass density rules) ---

        # OVERRIDE 1: Evidence photos detected
        # Sanity-check: if overall color density is near zero (< 0.015), the whole
        # page is essentially B&W.  A photo_regions hit at this density is a false
        # positive from a margin line or border decoration, not a real photograph.
        if features.photo_regions > 0 and features.color_density >= 0.015:
            return True
        
        # OVERRIDE 2: Charts/diagrams present (multiple = evidence)
        # Raised from >= 2 to >= 3 — two detected regions is still a common false
        # positive pattern (colored header + colored footer both pass as "charts").
        # Three independent validated charts is a much stronger evidence signal.
        if features.chart_regions >= 3:
            return True
        
        # OVERRIDE 3: Highlighted text (marked as important)
        if features.highlight_density > 0.001:
            return True
        
        # OVERRIDE 4: Stamps/seals present (official document marker)
        # Raised threshold from 0.0005 → 0.0020.
        # The stamp detector is now shape-aware (only compact blobs qualify),
        # so the mask is already filtered.  A real stamp blob of ~1,500 px² on a
        # typical 1.2M px² page gives density ≈ 0.0012.  Requiring 0.0020 means
        # the blob must be at least ~2,400 px² — a postage-stamp-sized ink mark.
        # Scattered colored text (many tiny components) produces density ≈ 0.000x
        # because each component fails the 1,500 px² minimum and is excluded.
        if features.stamp_density > 0.0020:
            return True

        # --- NON-COLOR OVERRIDES (v19: shape-based, saturation-agnostic) ---

        # OVERRIDE 5a: Colored annotation text (v20)
        # Red/orange attorney markings distributed across body text — legally meaningful
        # even when no stamps, photos, or highlights are detected.
        # Threshold 0.05 ≈ coverage 0.08% of body area — at least a short annotated sentence.
        if features.colored_annotation_density > 0.05:
            return True

        # OVERRIDE 5b: Grayscale image regions (B&W photos, scanned exhibits)
        if features.grayscale_regions > 0:
            return True

        # OVERRIDE 6: Handwritten signature patterns detected
        if features.signature_regions > 0:
            return True

        # OVERRIDE 7: B&W stamp/seal shapes detected
        if features.bw_stamp_regions > 0:
            return True

        # --- DENSITY RULES (no overrides triggered) ---
        
        # RULE 5: Insufficient color density → NOT meaningful
        # FIXED: Lowered threshold from 0.08 to 0.02, but allow meaningful elements to override
        if features.color_density < 0.02 and not any([
            features.photo_regions > 0,
            features.chart_regions >= 2,
            features.highlight_density > 0.001,
            features.stamp_density > 0.0020
        ]):
            return False
        
        # Check body color presence
        rich_body_color = features.body_fraction > 0.02
        
        # RULE 6: Decorative header only → NOT meaningful
        if features.header_fraction > 0.1 and features.body_fraction < 0.01:
            return False
        
        # RULE 7: Rich color in body → meaningful
        if features.color_density > self.config.rich_color_density and rich_body_color:
            return True
        
        # Default: low color presence → NOT meaningful
        return False
    
    def _get_meaningfulness_reason(self, features: VisualFeatures) -> str:
        """Get the reason for the meaningfulness determination with EXPLICIT RULE IDs."""
        # OVERRIDE REASONS (Rules 1-4)
        if features.photo_regions > 0 and features.color_density >= 0.015:
            return f"OVERRIDE_1_photo_evidence:{features.photo_regions}"
        if features.chart_regions >= 3:
            return f"OVERRIDE_2_chart_evidence:{features.chart_regions}"
        if features.highlight_density > 0.001:
            return f"OVERRIDE_3_highlighted_text:{features.highlight_density:.3f}"
        if features.stamp_density > 0.0020:
            return f"OVERRIDE_4_stamp_seal:{features.stamp_density:.3f}"
        if features.colored_annotation_density > 0.05:
            return f"OVERRIDE_5a_colored_annotation_text:{features.colored_annotation_density:.3f}"
        if features.grayscale_regions > 0:
            return f"OVERRIDE_5b_grayscale_image:{features.grayscale_regions}"
        if features.signature_regions > 0:
            return f"OVERRIDE_6_signature_pattern:{features.signature_regions}"
        if features.bw_stamp_regions > 0:
            return f"OVERRIDE_7_bw_stamp_seal:{features.bw_stamp_regions}"

        # DENSITY REASONS (Rules 5-7)
        if features.color_density < self.config.min_color_density:
            return f"RULE_5_insufficient_density:{features.color_density:.3f}<{self.config.min_color_density}"
        if features.header_fraction > 0.1 and features.body_fraction < 0.01:
            return "RULE_6_decorative_header_only"
        if features.color_density > self.config.rich_color_density:
            return f"RULE_7_rich_body_color:{features.color_density:.3f}"
        
        return "RULE_5_low_color_presence"
    
    def calculate_visual_score(self, features: VisualFeatures) -> float:
        """
        Calculate overall visual signal score (0-1).
        This score indicates the strength of visual signals, regardless of meaningfulness.
        """
        # Calculate score based on all visual features
        score = (
            0.4 * features.color_density +
            0.3 * features.spatial_distribution +
            0.2 * min(features.entropy / 5.0, 1.0) +
            0.1 * features.highlight_density +
            0.1 * min(features.photo_regions * 0.2, 1.0) +  # Bonus for photos
            0.1 * min(features.chart_regions * 0.2, 1.0)     # Bonus for charts
        )
        
        return float(np.clip(score, 0, 1))
