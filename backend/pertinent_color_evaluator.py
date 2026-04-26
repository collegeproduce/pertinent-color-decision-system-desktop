"""
Pertinent Color Evaluator - 2-Stage Exam
Per instruction.md Section 4

Evaluates color candidates to determine if color is pertinent (meaningful).
Uses deterministic, rule-based logic only - NO ML, NO semantic understanding.

NEW APPROACH: Two-Stage Exam
- Stage 1: Structural Color Significance (cheap, deterministic)
- Stage 2: Spatial Heuristics (area/position analysis)
- Stage 3: Vision OCR (placeholder for future)

PERTINENCE RULES (Locked):
✅ PERTINENT: Charts/graphs/diagrams, Highlighted text, Photos
❌ NOT PERTINENT: Logos/branding alone, Stamps/seals/annotations

Per instruction.md Section 4.1:
- Applies only to pages marked color_candidate = true
- Never revisits B&W-guaranteed pages
- Uses deterministic, rule-based logic only
"""

import re
from typing import List, Tuple, Dict, Optional
from models import PageRecord, PrintMode
import pymupdf  # PyMuPDF


class PertinentColorEvaluator:
    """
    Two-Stage Exam for Color Pertinence.
    
    Stage 1: Structural Analysis (Is color structural vs decorative?)
    Stage 2: Spatial Heuristics (Is color area significant?)
    
    Core Principle: Color must encode information, not identity or decoration.
    """
    
    def __init__(self):
        # Stage 2 thresholds (color area % of page)
        self.THRESHOLD_NOISE = 0.003  # 0.3% - below this is noise/stamps/logos
        self.THRESHOLD_AMBIGUOUS = 0.01  # 1.0% - ambiguous zone between 0.3-1.0%
        self.THRESHOLD_SIGNIFICANT = 0.01  # 1.0% - above this is visually significant
        
        # URL pattern for hyperlink detection
        self.url_pattern = re.compile(
            r'(https?://|www\.)[^\s]+\.(com|org|net|edu|gov|io|co)',
            re.IGNORECASE
        )
    
    def evaluate_page(self, page: pymupdf.Page, page_record: PageRecord) -> PageRecord:
        """
        Evaluate if color on a candidate page is pertinent.
        
        TWO-STAGE EXAM:
        1. Structural Analysis - filter decorative color
        2. Spatial Heuristics - verify significance
        
        Args:
            page: PyMuPDF page object
            page_record: PageRecord marked as color_candidate
            
        Returns:
            Updated PageRecord with final print decision
        """
        
        # Sanity check - should only evaluate color candidates
        if not page_record.color_candidate:
            return page_record
        
        # STAGE 1: Structural Color Significance
        stage1_result = self._stage1_structural_analysis(page)
        
        if stage1_result["failed"]:
            # Stage 1 hard fail - only logos/stamps/decoration
            page_record.final_print_mode = PrintMode.BW
            page_record.decision_details = f"Stage 1 FAIL: {stage1_result['reason']}"
            return page_record
        
        # STAGE 2: Spatial Heuristics
        stage2_result = self._stage2_spatial_analysis(page)
        
        # Decision logic combining Stage 1 + Stage 2
        color_area_pct = stage2_result["color_area_pct"]
        has_strong_signal = stage1_result["has_pertinent_elements"]
        
        # Zone 1: < 0.3% = Noise/stamps/branding → B&W
        if color_area_pct < self.THRESHOLD_NOISE:
            page_record.final_print_mode = PrintMode.BW
            page_record.decision_details = (
                f"Stage 2 FAIL: Color area {color_area_pct:.2%} < {self.THRESHOLD_NOISE:.2%} threshold "
                f"(likely noise/stamps/logos). Stage 1 signals: {stage1_result['signals']}"
            )
            return page_record
        
        # Zone 2: 0.3-1.0% = Ambiguous → Pass only with strong Stage 1 signal
        if color_area_pct < self.THRESHOLD_AMBIGUOUS:
            if has_strong_signal:
                page_record.finalize_as_color(
                    f"Stage 2 PASS (Ambiguous Zone): Color area {color_area_pct:.2%} with strong Stage 1 signal. "
                    f"Detected: {', '.join(stage1_result['signals'])}"
                )
            else:
                page_record.final_print_mode = PrintMode.BW
                page_record.decision_details = (
                    f"Stage 2 FAIL (Ambiguous Zone): Color area {color_area_pct:.2%} without strong pertinent signal. "
                    f"Stage 1 signals: {stage1_result['signals'] or 'None'}"
                )
            return page_record
        
        # Zone 3: ≥ 1.0% = Significant → Pass if Stage 1 allows
        if has_strong_signal:
            page_record.finalize_as_color(
                f"Stage 2 PASS: Color area {color_area_pct:.2%} ≥ {self.THRESHOLD_SIGNIFICANT:.2%} threshold "
                f"with pertinent elements. Detected: {', '.join(stage1_result['signals'])}"
            )
        else:
            # Large area but no pertinent signal - likely just colored backgrounds
            page_record.final_print_mode = PrintMode.BW
            page_record.decision_details = (
                f"Stage 2 FAIL: Color area {color_area_pct:.2%} ≥ threshold but no pertinent elements detected. "
                f"Likely decorative only."
            )
        
        return page_record
    
    # ============================================================
    # STAGE 1: Structural Color Significance Analysis
    # ============================================================
    
    def _stage1_structural_analysis(self, page: pymupdf.Page) -> Dict:
        """
        Stage 1: Determine if color is structural (meaningful) vs decorative.
        
        PASS CRITERIA (Pertinent):
        ✅ Charts/graphs/diagrams (color differentiates data)
        ✅ Highlighted text (color for emphasis)
        ✅ Photos (color adds evidentiary meaning)
        
        FAIL CRITERIA (Non-Pertinent):
        ❌ Logos/branding only
        ❌ Stamps/seals/markings/annotations only
        ❌ Hyperlinks only
        
        Returns:
            {
                "failed": bool,  # True if page has ONLY non-pertinent color
                "has_pertinent_elements": bool,  # True if any pertinent elements found
                "signals": List[str],  # Detected pertinent elements
                "reason": str  # Explanation
            }
        """
        result = {
            "failed": False,
            "has_pertinent_elements": False,
            "signals": [],
            "reason": ""
        }
        
        # Check for pertinent color elements
        has_charts = self._detect_charts_diagrams(page)
        has_highlights = self._detect_highlights(page)
        has_photos = self._detect_photos(page)
        has_hyperlinks = self._detect_hyperlinks(page)
        
        # Check for non-pertinent elements
        has_logos = self._detect_logos_branding(page)
        has_stamps = self._detect_stamps_annotations(page)
        
        # Build signal list
        if has_charts:
            result["signals"].append("charts/diagrams")
        if has_highlights:
            result["signals"].append("highlighted text")
        if has_photos:
            result["signals"].append("photos")
        
        # Determine if page has pertinent elements
        result["has_pertinent_elements"] = has_charts or has_highlights or has_photos
        
        # Stage 1 failure conditions:
        # 1. No pertinent elements AND has non-pertinent only
        # 2. Only hyperlinks
        if not result["has_pertinent_elements"]:
            if has_hyperlinks and not (has_logos or has_stamps):
                result["failed"] = True
                result["reason"] = "Only hyperlinks detected (non-pertinent)"
            elif (has_logos or has_stamps) and not has_hyperlinks:
                result["failed"] = True
                result["reason"] = "Only logos/stamps/branding detected (non-pertinent)"
            elif has_hyperlinks and (has_logos or has_stamps):
                result["failed"] = True
                result["reason"] = "Only hyperlinks and logos/stamps detected (non-pertinent)"
        
        return result
    
    def _detect_charts_diagrams(self, page: pymupdf.Page) -> bool:
        """
        Detect charts, graphs, or diagrams where color differentiates data.
        
        Heuristics:
        - Multiple colored vector objects (shapes, lines)
        - Distinct color groups (not just one color)
        - Geometric patterns suggesting data visualization
        """
        try:
            drawings = page.get_drawings()
            
            if not drawings or len(drawings) < 3:
                # Charts typically have multiple elements
                return False
            
            # Collect unique colors from vector graphics
            unique_colors = set()
            colored_shapes = 0
            
            for drawing in drawings:
                # Check fill color
                fill = drawing.get("fill", None)
                if fill and fill != (0, 0, 0) and fill != (1, 1, 1):  # Not black or white
                    unique_colors.add(fill)
                    colored_shapes += 1
                
                # Check stroke color
                color = drawing.get("color", None)
                if color and color != (0, 0, 0) and color != (1, 1, 1):  # Not black or white
                    unique_colors.add(color)
                    colored_shapes += 1
            
            # Chart indicator: Multiple colored shapes with 2+ distinct colors
            # This suggests color is being used to differentiate data
            if colored_shapes >= 3 and len(unique_colors) >= 2:
                return True
            
            return False
            
        except Exception:
            return False
    
    def _detect_highlights(self, page: pymupdf.Page) -> bool:
        """
        Detect highlighted text (color used for emphasis).
        
        Heuristics:
        - Colored text that is not hyperlinks
        - Background color annotations
        - Text with non-standard colors (not black/blue)
        """
        try:
            blocks = page.get_text("dict")["blocks"]
            
            colored_text_count = 0
            hyperlink_count = 0
            
            for block in blocks:
                if block.get("type") == 0:  # Text block
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            text = span.get("text", "").strip()
                            if not text:
                                continue
                            
                            color = span.get("color", 0)
                            
                            # Check if colored (not black)
                            if color != 0:
                                # Check if it's a hyperlink (blue)
                                if self._is_blue_color(color) and self.url_pattern.search(text):
                                    hyperlink_count += 1
                                else:
                                    # Non-hyperlink colored text = potential highlight
                                    colored_text_count += 1
            
            # If we have colored text that's not hyperlinks, likely highlights
            return colored_text_count > 0
            
        except Exception:
            return False
    
    def _detect_photos(self, page: pymupdf.Page) -> bool:
        """
        Detect photos where color adds evidentiary meaning.
        
        Heuristics:
        - Color images with high bit depth
        - Large images (not small logos/icons)
        - JPEG/photographic compression
        """
        try:
            images = page.get_images(full=True)
            
            for img_info in images:
                # img_info: (xref, smask, width, height, bpc, colorspace, alt, name, filter, bbox)
                if len(img_info) < 10:
                    continue
                
                width = img_info[2]
                height = img_info[3]
                bpc = img_info[4]  # bits per component
                colorspace = img_info[5]
                
                # Check if color image
                is_color = colorspace and colorspace not in ['DeviceGray', 'G', '/DeviceGray']
                
                # Check if substantial size (not tiny logo)
                # Minimum 100x100 pixels
                is_substantial = width >= 100 and height >= 100
                
                # Check if photographic quality (>= 8 bits per component)
                is_photo_quality = bpc >= 8
                
                if is_color and is_substantial and is_photo_quality:
                    return True
            
            return False
            
        except Exception:
            return False
    
    def _detect_hyperlinks(self, page: pymupdf.Page) -> bool:
        """
        Detect hyperlinks (non-pertinent color).
        
        Heuristics:
        - Blue colored text
        - URL patterns
        - Underlined text
        """
        try:
            blocks = page.get_text("dict")["blocks"]
            
            for block in blocks:
                if block.get("type") == 0:  # Text block
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            text = span.get("text", "").strip()
                            if not text:
                                continue
                            
                            color = span.get("color", 0)
                            
                            # Check for blue color and URL pattern
                            if self._is_blue_color(color) and self.url_pattern.search(text):
                                return True
            
            return False
            
        except Exception:
            return False
    
    def _detect_logos_branding(self, page: pymupdf.Page) -> bool:
        """
        Detect logos or branding (non-pertinent).
        
        Heuristics:
        - Small color images (< 100x100 pixels)
        - Images in margins/corners
        - Simple vector graphics (few elements)
        """
        try:
            images = page.get_images(full=True)
            
            # Get page dimensions for margin detection
            page_rect = page.rect
            page_width = page_rect.width
            page_height = page_rect.height
            
            margin_threshold = 0.15  # 15% from edges
            
            for img_info in images:
                if len(img_info) < 10:
                    continue
                
                width = img_info[2]
                height = img_info[3]
                colorspace = img_info[5]
                bbox = img_info[9] if len(img_info) > 9 else None
                
                # Check if color image
                is_color = colorspace and colorspace not in ['DeviceGray', 'G', '/DeviceGray']
                
                if not is_color:
                    continue
                
                # Check if small (typical logo size)
                is_small = width < 100 or height < 100
                
                # Check if in margins (if bbox available)
                in_margin = False
                if bbox:
                    x0, y0, x1, y1 = bbox[:4] if isinstance(bbox, (list, tuple)) else (0, 0, 0, 0)
                    # Check if near edges
                    near_left = x0 < page_width * margin_threshold
                    near_right = x1 > page_width * (1 - margin_threshold)
                    near_top = y0 < page_height * margin_threshold
                    near_bottom = y1 > page_height * (1 - margin_threshold)
                    in_margin = near_left or near_right or near_top or near_bottom
                
                if is_small or in_margin:
                    return True
            
            return False
            
        except Exception:
            return False
    
    def _detect_stamps_annotations(self, page: pymupdf.Page) -> bool:
        """
        Detect stamps, seals, or annotations (non-pertinent).
        
        Heuristics:
        - Annotations with color
        - Small colored regions with text overlay
        - Stamp-like patterns
        """
        try:
            # Check for PDF annotations
            annotations = page.annots()
            
            if annotations:
                for annot in annotations:
                    # Stamps and markup often have colors
                    if annot.type[0] in [13, 14, 15]:  # Stamp, ink, text markup
                        return True
            
            return False
            
        except Exception:
            return False
    
    def _is_blue_color(self, color_int: int) -> bool:
        """
        Check if color is blue-ish (typical hyperlink color).
        PyMuPDF color is stored as integer RGB (0xRRGGBB).
        """
        try:
            # Extract RGB components from integer
            r = (color_int >> 16) & 0xFF
            g = (color_int >> 8) & 0xFF
            b = color_int & 0xFF
            
            # Blue hyperlinks typically have:
            # - High blue component (> 150)
            # - Lower red and green components
            # - Blue > Red and Blue > Green
            return b > 150 and b > r and b > g
            
        except Exception:
            return False
    
    # ============================================================
    # STAGE 2: Spatial Heuristics Analysis
    # ============================================================
    
    def _stage2_spatial_analysis(self, page: pymupdf.Page) -> Dict:
        """
        Stage 2: Analyze color area coverage and spatial distribution.
        
        Calculates:
        - Total color area as % of page
        - Whether color is in central content vs margins
        - Distribution of color regions
        
        Thresholds:
        - < 0.3%: Noise/stamps/logos → Auto B&W
        - 0.3-1.0%: Ambiguous → Pass only with strong Stage 1 signal
        - ≥ 1.0%: Significant → Pass if Stage 1 allows
        
        Returns:
            {
                "color_area_pct": float,  # % of page covered by color
                "is_central": bool,  # True if color in central content
                "regions_count": int  # Number of distinct color regions
            }
        """
        try:
            # Get page dimensions
            page_rect = page.rect
            page_area = page_rect.width * page_rect.height
            
            if page_area == 0:
                return {"color_area_pct": 0.0, "is_central": False, "regions_count": 0}
            
            # Calculate color area from multiple sources
            total_color_area = 0.0
            color_regions = []
            
            # 1. Color from images
            images = page.get_images(full=True)
            for img_info in images:
                if len(img_info) < 10:
                    continue
                
                colorspace = img_info[5]
                bbox = img_info[9] if len(img_info) > 9 else None
                
                # Check if color image
                is_color = colorspace and colorspace not in ['DeviceGray', 'G', '/DeviceGray']
                
                if is_color and bbox:
                    # Calculate image area
                    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
                        x0, y0, x1, y1 = bbox[:4]
                        img_area = abs((x1 - x0) * (y1 - y0))
                        total_color_area += img_area
                        color_regions.append((x0, y0, x1, y1))
            
            # 2. Color from vector graphics (drawings)
            drawings = page.get_drawings()
            for drawing in drawings:
                # Check if drawing has color
                fill = drawing.get("fill", None)
                color = drawing.get("color", None)
                rect = drawing.get("rect", None)
                
                has_color = False
                if fill and fill != (0, 0, 0) and fill != (1, 1, 1):
                    has_color = True
                if color and color != (0, 0, 0) and color != (1, 1, 1):
                    has_color = True
                
                if has_color and rect:
                    # rect is a Rect object with x0, y0, x1, y1
                    drawing_area = abs(rect.width * rect.height)
                    total_color_area += drawing_area
                    color_regions.append((rect.x0, rect.y0, rect.x1, rect.y1))
            
            # Calculate percentage
            color_area_pct = total_color_area / page_area if page_area > 0 else 0.0
            
            # Check if color is central (not just in margins)
            is_central = self._is_color_central(page_rect, color_regions)
            
            return {
                "color_area_pct": color_area_pct,
                "is_central": is_central,
                "regions_count": len(color_regions)
            }
            
        except Exception as e:
            # If spatial analysis fails, return conservative estimate
            return {"color_area_pct": 0.01, "is_central": True, "regions_count": 1}
    
    def _is_color_central(self, page_rect, color_regions: List[Tuple]) -> bool:
        """
        Determine if color is in central content area (not just margins).
        
        Args:
            page_rect: Page bounding box
            color_regions: List of (x0, y0, x1, y1) tuples
        
        Returns:
            True if any color region overlaps with central area
        """
        if not color_regions:
            return False
        
        # Define central area (exclude 15% margins on all sides)
        margin_pct = 0.15
        central_x0 = page_rect.width * margin_pct
        central_y0 = page_rect.height * margin_pct
        central_x1 = page_rect.width * (1 - margin_pct)
        central_y1 = page_rect.height * (1 - margin_pct)
        
        # Check if any color region overlaps with central area
        for x0, y0, x1, y1 in color_regions:
            # Check for overlap
            overlaps = not (x1 < central_x0 or x0 > central_x1 or 
                           y1 < central_y0 or y0 > central_y1)
            if overlaps:
                return True
        
        return False

