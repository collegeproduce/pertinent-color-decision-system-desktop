"""
Page-Level Metadata Extraction Orchestrator
Per instruction.md Section 2

Coordinates Tier 1 → Tier 2 → Tier 3 with Rule of Certainty:
- Stop immediately when B&W is guaranteed
- Escalate when color is detected
- No false negatives allowed
"""

from models import PageRecord, DocumentResult
from tier1_tier2 import Tier1ColorspaceInspector, Tier2ImageMetadataInspector
from tier3 import Tier3RasterProbe
import pymupdf  # PyMuPDF


class MetadataExtractor:
    """
    Main coordinator for page-level metadata extraction.
    
    Per instruction.md Section 2:
    - Runs Tier 1 → Tier 2 → Tier 3 in sequence
    - Applies Rule of Certainty (Section 2.1)
    - Stops on first B&W guarantee
    - Escalates on color detection
    """
    
    def __init__(self):
        self.tier1 = Tier1ColorspaceInspector()
        self.tier2 = Tier2ImageMetadataInspector()
        self.tier3 = Tier3RasterProbe(dpi=72, sample_rate=20, tolerance=5)
    
    def extract_page_metadata(self, page: pymupdf.Page, page_record: PageRecord) -> PageRecord:
        """
        Extract metadata for a single page using 3-tier approach.
        
        Per instruction.md Section 2.1 - Rule of Certainty:
        "If a page is guaranteed black & white, it:
         - Is immediately finalized as B&W
         - Is never analyzed again
         - Never enters downstream logic
         - No exceptions. No overrides."
        
        Args:
            page: PyMuPDF page object
            page_record: PageRecord to populate
            
        Returns:
            Updated PageRecord with metadata decision
        """
        
        # Tier 1: PDF Structural Colorspace Inspection (Section 2.2)
        tier1_bw_confirmed = self.tier1.inspect_page(page, page_record)
        
        if tier1_bw_confirmed:
            # B&W guaranteed - STOP processing
            # Per instruction.md: "STOP processing this page"
            return page_record
        
        # Tier 1 inconclusive - proceed to Tier 2
        
        # Tier 2: Embedded Image Metadata Inspection (Section 2.3)
        tier2_bw_confirmed = self.tier2.inspect_page(page, page_record)
        
        if tier2_bw_confirmed:
            # B&W guaranteed - STOP processing
            # Per instruction.md: "STOP processing this page"
            return page_record
        
        # Tier 2 inconclusive - proceed to Tier 3
        
        # Tier 3: Low-Cost Raster Probe (Section 2.4)
        color_detected = self.tier3.inspect_page(page, page_record)
        
        if not color_detected:
            # B&W confirmed by raster - STOP processing
            return page_record
        
        # Color detected - page is now a color candidate
        # Per instruction.md: "Proceed to downstream evaluation"
        # Page already marked as color_candidate by tier3
        
        return page_record
    
    def extract_document_metadata(self, pdf_path: str) -> DocumentResult:
        """
        Extract metadata for all pages in a PDF document.
        
        Per instruction.md Section 1 and 2:
        - Initialize page ledger (Section 1.2)
        - Process each page independently
        - Apply early elimination
        
        Args:
            pdf_path: Path to PDF file
            
        Returns:
            DocumentResult with all page records
        """
        doc = pymupdf.open(pdf_path)
        total_pages = len(doc)
        
        result = DocumentResult(total_pages=total_pages)
        
        # Process each page independently (per instruction.md Section 1.2)
        for page_num in range(total_pages):
            page = doc[page_num]
            
            # Initialize page record
            page_record = PageRecord(page_id=page_num + 1)  # 1-indexed for humans
            
            # Extract metadata (Tier 1 → 2 → 3)
            self.extract_page_metadata(page, page_record)
            
            result.pages.append(page_record)
        
        doc.close()
        
        return result
