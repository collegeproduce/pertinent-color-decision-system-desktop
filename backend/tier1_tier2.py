"""
Tier 1: PDF Structural Colorspace Inspection
Per instruction.md Section 2.2

Inspects page content streams for colorspace usage WITHOUT rasterization.
Fastest elimination method - kills 60-75% of pages instantly.
"""

from typing import Optional
from models import PageRecord, MetadataSource
import pymupdf  # PyMuPDF


class Tier1ColorspaceInspector:
    """
    PDF operator & colorspace scanner.
    Detects DeviceGray vs RGB/CMYK from page content streams.
    Zero pixel processing.
    """
    
    def __init__(self):
        # Colorspace indicators that signal non-grayscale content
        self.color_indicators = [
            b'/DeviceRGB',
            b'/DeviceCMYK',
            b'/ICCBased',
            b'/CalRGB',
            b'/Lab',
            b'/Separation',
            b'/DeviceN'
        ]
    
    def _has_pertinent_color_annotations(self, page: pymupdf.Page) -> bool:
        """
        Check for pertinent color annotations BEFORE finalizing as B&W.
        
        Critical: Highlights are PERTINENT color but stored as annotations,
        not in page content. We must check these before declaring B&W.
        
        Returns:
            True if pertinent color annotations found (highlights, colored markup)
            False otherwise
        """
        try:
            annots = page.annots()
            if not annots:
                return False
            
            for annot in annots:
                annot_type = annot.type[0]
                
                # Type 8 = Highlight annotation (PERTINENT - used for emphasis)
                if annot_type == 8:
                    # Check if highlight has color (not just grayscale)
                    colors = annot.colors
                    if colors and len(colors.get('stroke', [])) >= 3:
                        r, g, b = colors['stroke'][:3]
                        # If not grayscale (R≠G≠B beyond small tolerance)
                        if not (abs(r - g) < 0.05 and abs(g - b) < 0.05):
                            return True
                
                # Type 9 = Underline (could be for emphasis)
                # Type 10 = Strikeout (could be for emphasis)
                # Type 11 = Squiggly (could be for emphasis)
                if annot_type in [9, 10, 11]:
                    colors = annot.colors
                    if colors and len(colors.get('stroke', [])) >= 3:
                        r, g, b = colors['stroke'][:3]
                        if not (abs(r - g) < 0.05 and abs(g - b) < 0.05):
                            return True
                
                # Type 14 = Ink annotation (colored markup)
                if annot_type == 14:
                    colors = annot.colors
                    if colors:
                        stroke = colors.get('stroke', [])
                        if len(stroke) >= 3:
                            r, g, b = stroke[:3]
                            if not (abs(r - g) < 0.05 and abs(g - b) < 0.05):
                                return True
            
            return False
            
        except Exception as e:
            # If check fails, assume no pertinent annotations (safe default)
            return False
    
    def inspect_page(self, page: pymupdf.Page, page_record: PageRecord) -> bool:
        """
        Inspect page for colorspace usage.
        
        Returns:
            True if page is guaranteed B&W (early exit)
            False if color is possible (proceed to Tier 2)
        
        Per instruction.md Section 2.2:
        - If only DeviceGray: bw_guaranteed = true, STOP
        - If non-gray colorspace: Proceed to Tier 2
        """
        try:
            # Get page content stream as bytes
            content = page.read_contents()
            
            # Check for color colorspaces in content stream
            has_color = False
            for indicator in self.color_indicators:
                if indicator in content:
                    has_color = True
                    break
            
            # Additional check: examine the page's resources
            if not has_color:
                has_color = self._check_page_resources(page)
            
            if not has_color:
                # CRITICAL: Check for pertinent color annotations (highlights, markup)
                # before finalizing as B&W. Highlights are PERTINENT but stored
                # as annotations, not in content stream.
                if self._has_pertinent_color_annotations(page):
                    # Has color highlights/markup - cannot be B&W
                    return False  # Proceed to Tier 2
                
                # Only DeviceGray detected and no pertinent annotations - guaranteed B&W
                page_record.finalize_as_bw(
                    MetadataSource.PDF_COLORSPACE,
                    "Only DeviceGray colorspace detected in page content (no pertinent annotations)"
                )
                return True  # Early exit - STOP processing
            
            # Color colorspace detected - cannot guarantee B&W
            return False  # Proceed to Tier 2
            
        except Exception as e:
            # If inspection fails, proceed to next tier (no false negatives)
            page_record.decision_details = f"Tier 1 failed: {str(e)}"
            return False
    
    def _check_page_resources(self, page: pymupdf.Page) -> bool:
        """
        Check page resources dictionary for color colorspaces.
        PyMuPDF provides structured access to resources.
        """
        try:
            # Get all images on the page
            image_list = page.get_images(full=True)
            
            for img_info in image_list:
                # img_info is a tuple: (xref, smask, width, height, bpc, colorspace, alt, name, filter, bbox)
                if len(img_info) > 5:
                    colorspace = img_info[5]  # Index 5 is colorspace
                    if colorspace and colorspace not in ['DeviceGray', 'G', '']:
                        return True  # Non-gray colorspace found
            
            return False
            
        except Exception:
            # If resource check fails, assume color possible (no false negatives)
            return True


class Tier2ImageMetadataInspector:
    """
    Tier 2: Embedded Image Header Inspection
    Per instruction.md Section 2.3
    
    Only applied if Tier 1 doesn't guarantee B&W.
    Reads image dictionaries WITHOUT decoding pixels.
    """
    
    def inspect_page(self, page: pymupdf.Page, page_record: PageRecord) -> bool:
        """
        Inspect embedded image metadata.
        
        Returns:
            True if page is guaranteed B&W (early exit)
            False if color is possible (proceed to Tier 3)
        
        Per instruction.md Section 2.3:
        - If all images DeviceGray/1-bit/masks: bw_guaranteed = true, STOP
        - If any RGB/CMYK image: Proceed to Tier 3
        """
        try:
            image_list = page.get_images(full=True)
            
            # If no images, cannot determine from this tier
            if not image_list:
                return False  # Proceed to Tier 3
            
            # Check each image's metadata
            all_images_bw = True
            for img_info in image_list:
                if not self._is_image_bw(img_info, page):
                    all_images_bw = False
                    break
            
            if all_images_bw:
                # CRITICAL: Check for pertinent color annotations before finalizing
                # Use the same method from Tier1 inspector
                tier1_inspector = Tier1ColorspaceInspector()
                if tier1_inspector._has_pertinent_color_annotations(page):
                    # Has color highlights/markup - cannot be B&W
                    return False  # Proceed to Tier 3
                
                # All images are B&W and no pertinent annotations - guaranteed B&W page
                page_record.finalize_as_bw(
                    MetadataSource.IMAGE_HEADER,
                    f"All {len(image_list)} embedded images are DeviceGray/1-bit/mask (no pertinent annotations)"
                )
                return True  # Early exit - STOP processing
            
            # At least one color image - cannot guarantee B&W
            return False  # Proceed to Tier 3
            
        except Exception as e:
            # If inspection fails, proceed to next tier (no false negatives)
            page_record.decision_details = f"Tier 2 failed: {str(e)}"
            return False
    
    def _is_image_bw(self, img_info: tuple, page: pymupdf.Page) -> bool:
        """
        Check if image is B&W based on metadata only.
        
        Per instruction.md Section 2.3 decision rules:
        - DeviceGray colorspace
        - 1-bit images
        - Image masks
        """
        try:
            # img_info tuple: (xref, smask, width, height, bpc, colorspace, alt, name, filter, bbox)
            xref = img_info[0]
            colorspace = img_info[5] if len(img_info) > 5 else None
            bpc = img_info[4] if len(img_info) > 4 else None  # bits per component
            
            # Check colorspace
            if colorspace in ['DeviceGray', 'G', '/DeviceGray']:
                return True
            
            # Check if 1-bit image
            if bpc == 1:
                return True
            
            # Check if it's an image mask
            # Get more detailed info from xref
            try:
                img_dict = page.parent.xref_object(xref)
                if '/ImageMask' in img_dict or 'ImageMask' in img_dict:
                    return True
            except:
                pass
            
            return False
            
        except Exception:
            # If check fails, assume color possible (no false negatives)
            return False
