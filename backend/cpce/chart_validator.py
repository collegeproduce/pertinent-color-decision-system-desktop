"""
CPCE v5 - Chart Validation Module
Validates detected charts to reduce false positives from decorative elements.
Checks for: axes, labels, data points, grid lines.
"""
import cv2
import numpy as np
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass


@dataclass
class ChartValidationResult:
    """Result of chart validation."""
    is_valid_chart: bool
    has_x_axis: bool
    has_y_axis: bool
    has_labels: bool
    has_data_points: bool
    has_grid_lines: bool
    confidence_score: float  # 0-1
    rejection_reason: str = ""


class ChartValidator:
    """
    Validates chart detections to reduce false positives.
    
    Problem: OpenCV contour detection over-triggers on decorative elements
    that look like charts but aren't (borders, boxes, etc.)
    
    Solution: Check for actual chart features:
    - Axes (horizontal/vertical lines with tick marks)
    - Labels (text along axes)
    - Data points (dots, bars, lines)
    - Grid lines (parallel lines)
    """
    
    def __init__(self):
        self.min_axis_length_ratio = 0.3  # Axis must be at least 30% of image width/height
        self.min_data_points = 3
        self.min_grid_lines = 2
    
    def validate(self, img: np.ndarray, region_bbox: Tuple[int, int, int, int]) -> ChartValidationResult:
        """
        Validate if a detected region is actually a chart.
        
        Args:
            img: Full page image
            region_bbox: (x, y, width, height) of detected chart region
            
        Returns:
            ChartValidationResult with validation details
        """
        x, y, w, h = region_bbox

        # v17: Minimum size pre-check — eliminates icon bars, thin banners, avatars
        # A real chart needs sufficient height AND area to contain axes + labels + data
        if min(w, h) < 60 or w * h < 8000:
            return ChartValidationResult(
                is_valid_chart=False,
                has_x_axis=False,
                has_y_axis=False,
                has_labels=False,
                has_data_points=False,
                has_grid_lines=False,
                confidence_score=0.0,
                rejection_reason=f"Region too small ({w}x{h}) to be a chart"
            )

        # v17: Aspect ratio gate — icon rows are very wide and very short (ratio > 6)
        # Real charts don't typically exceed 5:1 width-to-height
        aspect = w / max(h, 1)
        if aspect > 6.0:
            return ChartValidationResult(
                is_valid_chart=False,
                has_x_axis=False,
                has_y_axis=False,
                has_labels=False,
                has_data_points=False,
                has_grid_lines=False,
                confidence_score=0.0,
                rejection_reason=f"Aspect ratio {aspect:.1f} too extreme for a chart (icon bar)"
            )

        # Extract the region
        region = img[y:y+h, x:x+w]
        if region.size == 0:
            return ChartValidationResult(
                is_valid_chart=False,
                has_x_axis=False,
                has_y_axis=False,
                has_labels=False,
                has_data_points=False,
                has_grid_lines=False,
                confidence_score=0.0,
                rejection_reason="Empty region"
            )
        
        # Convert to grayscale if needed
        if len(region.shape) == 3:
            gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
        else:
            gray = region
        
        # Edge detection
        edges = cv2.Canny(gray, 50, 150)
        
        # Check for axes
        has_x_axis, has_y_axis = self._detect_axes(edges, w, h)
        
        # Check for grid lines
        has_grid_lines = self._detect_grid_lines(edges, w, h)
        
        # Check for data points (bars, dots, lines)
        has_data_points = self._detect_data_points(gray, edges)
        
        # Check for labels (text along edges)
        has_labels = self._detect_labels(gray, edges, w, h)
        
        # Calculate confidence score
        confidence = self._calculate_confidence(
            has_x_axis, has_y_axis, has_labels, 
            has_data_points, has_grid_lines
        )
        
        # Raised from 0.5 → 0.65.  Single-axis + data_points alone scores 0.50,
        # which now correctly fails.  Both axes + data_points = 0.70 → passes.
        is_valid = confidence >= 0.65

        # Generate rejection reason if invalid
        rejection_reason = ""
        if not is_valid:
            missing = []
            if not has_x_axis and not has_y_axis:
                missing.append("no axes detected")
            elif not has_x_axis:
                missing.append("no x-axis")
            elif not has_y_axis:
                missing.append("no y-axis")
            if not has_data_points:
                missing.append("no data points")
            if confidence < 0.4:
                missing.append("insufficient chart features")
            rejection_reason = "; ".join(missing) if missing else f"low confidence ({confidence:.2f} < 0.65)"
        
        return ChartValidationResult(
            is_valid_chart=is_valid,
            has_x_axis=has_x_axis,
            has_y_axis=has_y_axis,
            has_labels=has_labels,
            has_data_points=has_data_points,
            has_grid_lines=has_grid_lines,
            confidence_score=confidence,
            rejection_reason=rejection_reason
        )
    
    def _detect_axes(self, edges: np.ndarray, width: int, height: int) -> Tuple[bool, bool]:
        """Detect horizontal (x) and vertical (y) axes."""
        # Hough line transform
        lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=30, 
                                minLineLength=min(width, height) * 0.2, 
                                maxLineGap=10)
        
        if lines is None:
            return False, False
        
        has_x_axis = False
        has_y_axis = False
        
        for line in lines:
            x1, y1, x2, y2 = line[0]
            
            # Calculate angle
            angle = np.abs(np.arctan2(y2 - y1, x2 - x1) * 180.0 / np.pi)
            
            # Horizontal line (x-axis candidate) - angle near 0 or 180
            if angle < 10 or angle > 170:
                line_length = np.sqrt((x2-x1)**2 + (y2-y1)**2)
                if line_length > width * self.min_axis_length_ratio:
                    has_x_axis = True
            
            # Vertical line (y-axis candidate) - angle near 90
            if 80 < angle < 100:
                line_length = np.sqrt((x2-x1)**2 + (y2-y1)**2)
                if line_length > height * self.min_axis_length_ratio:
                    has_y_axis = True
        
        return has_x_axis, has_y_axis
    
    def _detect_grid_lines(self, edges: np.ndarray, width: int, height: int) -> bool:
        """Detect parallel grid lines (characteristic of charts)."""
        lines = cv2.HoughLinesP(edges, 1, np.pi/180, threshold=20,
                                minLineLength=min(width, height) * 0.15,
                                maxLineGap=5)
        
        if lines is None or len(lines) < 2:
            return False
        
        # Count parallel lines
        horizontal_lines = 0
        vertical_lines = 0
        
        for line in lines:
            x1, y1, x2, y2 = line[0]
            angle = np.abs(np.arctan2(y2 - y1, x2 - x1) * 180.0 / np.pi)
            
            if angle < 10 or angle > 170:
                horizontal_lines += 1
            elif 80 < angle < 100:
                vertical_lines += 1
        
        # Need at least 2 parallel lines in one direction (grid pattern)
        return horizontal_lines >= self.min_grid_lines or vertical_lines >= self.min_grid_lines
    
    def _detect_data_points(self, gray: np.ndarray, edges: np.ndarray) -> bool:
        """Detect data points (bars, dots, line segments).

        Key constraint: individual text characters have areas of ~20-150 px².
        The lower bound is raised to 300 px² so that a text-heavy region (legal
        body text, headers, footers) cannot masquerade as chart data.
        """
        valid_data_shapes = 0

        # Method 1: Substantial edge-contours (bars, pie slices, dot markers)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if 300 < area < 8000:  # Raised lower bound from 50 → 300 to exclude text chars
                valid_data_shapes += 1

        # Method 2: Filled rectangular regions (bar-chart bars)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        filled_contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for cnt in filled_contours:
            x, y, w, h = cv2.boundingRect(cnt)
            aspect_ratio = float(w) / h if h > 0 else 0
            # Bars: reasonable aspect ratio AND area large enough to be a real bar
            if 0.2 < aspect_ratio < 5 and 600 < w * h < 12000:
                valid_data_shapes += 1

        return valid_data_shapes >= self.min_data_points
    
    def _detect_labels(self, gray: np.ndarray, edges: np.ndarray, width: int, height: int) -> bool:
        """Detect axis labels along chart edges.

        A chart has labels on ≥ 2 distinct sides (e.g. x-axis bottom + y-axis left).
        A plain text page or colored box has text everywhere — it would score all 4
        borders, which is exactly what we want to reject.  So we require:
          - text-like evidence on exactly 2-3 sides (chart pattern), OR
          - text on the bottom AND at least one lateral side (typical axis layout).
        Scoring all 4 borders ≥ text threshold is a disqualifier (body-text page).
        """
        border_regions = [
            (gray[0:int(height * 0.15), :],          'top'),
            (gray[-int(height * 0.15):, :],           'bottom'),
            (gray[:, 0:int(width * 0.15)],            'left'),
            (gray[:, -int(width * 0.15):],            'right'),
        ]

        sides_with_labels = []
        for border, side in border_regions:
            if border.size == 0:
                continue
            side_score = 0

            # High local variance → text-like texture
            if np.var(border) > 500:
                side_score += 1

            # Small connected components → individual characters
            _, binary = cv2.threshold(border, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            small_components = sum(1 for cnt in contours if 20 < cv2.contourArea(cnt) < 500)
            if small_components >= 3:
                side_score += 1

            if side_score >= 2:
                sides_with_labels.append(side)

        n = len(sides_with_labels)
        # All 4 sides have text → body text page, not a chart → reject
        if n >= 4:
            return False
        # Chart pattern: labels on 2 or 3 sides
        if 2 <= n <= 3:
            return True
        return False
    
    def _calculate_confidence(self, has_x_axis: bool, has_y_axis: bool,
                             has_labels: bool, has_data_points: bool,
                             has_grid_lines: bool) -> float:
        """Calculate overall chart confidence score.

        Threshold raised from 0.5 → 0.65 (see validate()).
        Single-axis + data_points alone only reaches 0.50, which now falls below
        threshold.  A real chart needs BOTH axes (0.40) + data_points (0.30) = 0.70,
        OR one axis (0.20) + data_points (0.30) + labels (0.15) + grid (0.15) = 0.80.
        This prevents colored text boxes, headers, and footers from passing.
        """
        score = 0.0

        # Axes are the primary discriminator
        if has_x_axis and has_y_axis:
            score += 0.40
        elif has_x_axis or has_y_axis:
            score += 0.20

        # Data points are essential
        if has_data_points:
            score += 0.30

        # Labels add credibility
        if has_labels:
            score += 0.15

        # Grid lines suggest structured chart
        if has_grid_lines:
            score += 0.15

        return min(1.0, score)
    
    def batch_validate(self, img: np.ndarray, 
                      chart_regions: List[Tuple[int, int, int, int]]) -> List[ChartValidationResult]:
        """Validate multiple chart regions at once."""
        return [self.validate(img, region) for region in chart_regions]
