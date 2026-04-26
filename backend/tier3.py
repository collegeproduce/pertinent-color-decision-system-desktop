"""
Tier 3: Low-Cost Raster Color Probe
Per instruction.md Section 2.4

Fallback only when Tier 1 and Tier 2 are inconclusive.
Renders page at very low DPI and samples pixels for color detection.
"""

from typing import Optional
from models import PageRecord, MetadataSource
import pymupdf  # PyMuPDF
from PIL import Image
import io


class Tier3RasterProbe:
    """
    Ultra-cheap raster color detection.
    
    Per instruction.md Section 2.4:
    - Render at low DPI (50-72)
    - Sample small subset of pixels
    - Check for non-grayscale beyond tolerance
    - No semantic analysis, just pixel math
    """
    
    def __init__(self, dpi: int = 72, sample_rate: int = 20, tolerance: int = 5):
        """
        Args:
            dpi: Render resolution (50-72 recommended)
            sample_rate: Sample every Nth pixel (e.g., 20 = every 20th pixel)
            tolerance: RGB difference tolerance (if R≈G≈B within this, consider grayscale)
        """
        self.dpi = dpi
        self.sample_rate = sample_rate
        self.tolerance = tolerance
    
    def inspect_page(self, page: pymupdf.Page, page_record: PageRecord) -> bool:
        """
        Perform low-cost raster color detection.
        
        Returns:
            True if color detected (mark as candidate)
            False if no color detected (finalize as B&W)
        
        Per instruction.md Section 2.4:
        - If no color detected: bw_guaranteed = true, STOP
        - If color detected: color_candidate = true, proceed to downstream
        """
        try:
            # Render page at low DPI
            pix = page.get_pixmap(dpi=self.dpi)
            
            # Convert to PIL Image for easier pixel access
            img_data = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_data))
            
            # Convert to RGB if not already
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Sample pixels
            width, height = img.size
            pixels_sampled = 0
            color_pixels_found = 0
            
            for y in range(0, height, self.sample_rate):
                for x in range(0, width, self.sample_rate):
                    pixels_sampled += 1
                    r, g, b = img.getpixel((x, y))
                    
                    # Check if pixel is truly color (R ≠ G ≠ B beyond tolerance)
                    if self._is_color_pixel(r, g, b):
                        color_pixels_found += 1
                        
                        # Early exit optimization: if we find color, no need to continue
                        # Mark as color candidate and proceed downstream
                        page_record.mark_as_color_candidate(
                            MetadataSource.RASTER_PROBE,
                            f"Color detected at low DPI scan ({color_pixels_found}/{pixels_sampled} sampled pixels)"
                        )
                        return True  # Color found - escalate to Phase 4
            
            # No color detected in raster sampling
            # CRITICAL: Before finalizing as B&W, check for pertinent annotations
            # Import here to avoid circular dependency
            from tier1_tier2 import Tier1ColorspaceInspector
            
            tier1_inspector = Tier1ColorspaceInspector()
            if tier1_inspector._has_pertinent_color_annotations(page):
                # Has color highlights/markup - escalate to pertinence evaluation
                page_record.mark_as_color_candidate(
                    MetadataSource.RASTER_PROBE,
                    "Pertinent color annotations detected (highlights/markup) after B&W raster scan"
                )
                return True  # Escalate to Phase 4
            
            # No color detected and no pertinent annotations - guaranteed B&W
            page_record.finalize_as_bw(
                MetadataSource.RASTER_PROBE,
                f"No color detected in raster scan ({pixels_sampled} pixels sampled at DPI {self.dpi}), no pertinent annotations"
            )
            return False  # B&W confirmed - STOP processing
            
        except Exception as e:
            # If raster probe fails, escalate to be safe (no false negatives)
            page_record.mark_as_color_candidate(
                MetadataSource.RASTER_PROBE,
                f"Tier 3 raster probe failed: {str(e)} - escalating for safety"
            )
            return True  # Escalate when uncertain
    
    def _is_color_pixel(self, r: int, g: int, b: int) -> bool:
        """
        Determine if a pixel is truly color or effectively grayscale.
        
        Per instruction.md: Check if R ≠ G ≠ B beyond tolerance
        
        Returns:
            True if pixel is color
            False if pixel is grayscale (R ≈ G ≈ B)
        """
        # Calculate max difference between RGB components
        max_diff = max(abs(r - g), abs(g - b), abs(r - b))
        
        # If difference exceeds tolerance, it's color
        return max_diff > self.tolerance
