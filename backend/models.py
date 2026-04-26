"""
Core data models for the Page-Level Color Printing Decision System.
Aligned with instruction.md Section 1.2
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List


class PrintMode(Enum):
    """Final print decision for a page"""
    BW = "B&W"
    COLOR = "Color"
    UNDECIDED = "undecided"


class MetadataSource(Enum):
    """Source of the metadata decision - for auditability"""
    PDF_COLORSPACE = "pdf_colorspace"      # Tier 1
    IMAGE_HEADER = "image_header"          # Tier 2
    RASTER_PROBE = "raster_probe"          # Tier 3
    PERTINENCE_RULE = "pertinence_rule"    # Phase 4


@dataclass
class PageRecord:
    """
    Page-level record tracking decision state.
    Per instruction.md Section 1.2 - each page treated independently.
    """
    page_id: int
    bw_guaranteed: Optional[bool] = None
    color_candidate: Optional[bool] = None
    final_print_mode: PrintMode = PrintMode.UNDECIDED
    metadata_source: Optional[MetadataSource] = None
    decision_details: str = ""  # Additional context for debugging
    
    def finalize_as_bw(self, source: MetadataSource, reason: str = ""):
        """
        Mark page as guaranteed B&W. Per instruction.md Section 2.1:
        - Immediately finalized
        - Never analyzed again
        - Never enters downstream logic
        """
        self.bw_guaranteed = True
        self.color_candidate = False
        self.final_print_mode = PrintMode.BW
        self.metadata_source = source
        self.decision_details = reason
        
    def mark_as_color_candidate(self, source: MetadataSource, reason: str = ""):
        """
        Mark page as potential color - must proceed to downstream evaluation.
        Per instruction.md Section 2.4 and 4.0
        """
        self.bw_guaranteed = False
        self.color_candidate = True
        self.metadata_source = source
        self.decision_details = reason
        
    def finalize_as_color(self, reason: str = ""):
        """
        Mark page as pertinent color after evaluation.
        Per instruction.md Section 4.2
        """
        self.final_print_mode = PrintMode.COLOR
        self.decision_details = reason


@dataclass
class DocumentResult:
    """
    Aggregated result for entire document.
    Per instruction.md Section 3 and 5
    """
    total_pages: int
    pages: List[PageRecord] = field(default_factory=list)
    
    def get_color_pages(self) -> List[int]:
        """Return list of page IDs that must be printed in color"""
        return [p.page_id for p in self.pages if p.final_print_mode == PrintMode.COLOR]
    
    def get_bw_pages(self) -> List[int]:
        """Return list of page IDs that are B&W"""
        return [p.page_id for p in self.pages if p.final_print_mode == PrintMode.BW]
    
    def is_all_bw(self) -> bool:
        """Check if entire document is B&W - optimization per instruction.md Section 3"""
        return all(p.final_print_mode == PrintMode.BW for p in self.pages)
    
    def get_color_candidates(self) -> List[PageRecord]:
        """Return pages that need pertinent color evaluation"""
        return [p for p in self.pages if p.color_candidate is True]
    
    def to_output_dict(self) -> dict:
        """
        Generate final output per instruction.md Section 5:
        - page_id
        - final_print_mode
        - decision_basis
        """
        return {
            "total_pages": self.total_pages,
            "color_pages": self.get_color_pages(),
            "bw_pages": self.get_bw_pages(),
            "page_details": [
                {
                    "page_id": p.page_id,
                    "final_print_mode": p.final_print_mode.value,
                    "decision_basis": p.metadata_source.value if p.metadata_source else "unknown",
                    "details": p.decision_details
                }
                for p in self.pages
            ]
        }
