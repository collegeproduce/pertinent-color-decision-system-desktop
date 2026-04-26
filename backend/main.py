"""
Main Pipeline - Page-Level Color Printing Decision System
Per instruction.md Sections 0-7

End-to-end pipeline that:
1. Extracts page-level metadata (Tier 1→2→3)
2. Aggregates document-level results
3. Evaluates pertinent color for candidates only
4. Generates final output

Guarantees (per instruction.md Section 6):
- Early elimination
- No false negatives
- Determinism (same input → same output)
- Auditability (every decision has a source)
"""

import json
from pathlib import Path
from typing import Union
from models import DocumentResult, PrintMode
from metadata_extractor import MetadataExtractor
from pertinent_color_evaluator import PertinentColorEvaluator
import pymupdf  # PyMuPDF


class ColorPrintingDecisionPipeline:
    """
    Main pipeline orchestrating entire decision flow.
    Per instruction.md Section 0-7.
    """
    
    def __init__(self):
        self.metadata_extractor = MetadataExtractor()
        self.pertinent_evaluator = PertinentColorEvaluator()
    
    def process_document(self, pdf_path: Union[str, Path]) -> DocumentResult:
        """
        Process PDF document through complete decision pipeline.
        
        Per instruction.md flow:
        1. Extract page-level metadata (Section 2)
        2. Document-level aggregation (Section 3)
        3. Pertinent color evaluation for candidates only (Section 4)
        4. Generate final output (Section 5)
        
        Args:
            pdf_path: Path to PDF file
            
        Returns:
            DocumentResult with final decisions for all pages
        """
        pdf_path = str(pdf_path)
        
        # Phase 1: Page-Level Metadata Extraction
        # Per instruction.md Section 2 - Tier 1 → 2 → 3 with early elimination
        print(f"📄 Processing: {Path(pdf_path).name}")
        print(f"⚙️  Phase 1: Metadata Extraction (Tier 1→2→3)")
        
        result = self.metadata_extractor.extract_document_metadata(pdf_path)
        
        print(f"   Total pages: {result.total_pages}")
        print(f"   B&W guaranteed: {len([p for p in result.pages if p.bw_guaranteed])}")
        print(f"   Color candidates: {len(result.get_color_candidates())}")
        
        # Phase 2: Document-Level Aggregation (Optimization)
        # Per instruction.md Section 3
        print(f"\n⚙️  Phase 2: Document-Level Aggregation")
        
        if result.is_all_bw():
            print(f"   ✅ All pages B&W - no further processing needed")
            return result
        
        print(f"   ⚠️  Mixed document - proceeding to pertinent color evaluation")
        
        # Phase 3: Pertinent Color Evaluation
        # Per instruction.md Section 4 - ONLY for color candidates
        print(f"\n⚙️  Phase 3: Pertinent Color Evaluation")
        
        candidates = result.get_color_candidates()
        print(f"   Evaluating {len(candidates)} color candidate pages...")
        
        # Re-open PDF for pertinent color evaluation
        doc = pymupdf.open(pdf_path)
        
        for page_record in candidates:
            page = doc[page_record.page_id - 1]  # Convert to 0-indexed
            self.pertinent_evaluator.evaluate_page(page, page_record)
        
        doc.close()
        
        # Phase 4: Final Summary
        print(f"\n✅ Processing Complete")
        print(f"   Color pages: {len(result.get_color_pages())}")
        print(f"   B&W pages: {len(result.get_bw_pages())}")
        
        return result
    
    def generate_report(self, result: DocumentResult, output_path: Union[str, Path] = None) -> dict:
        """
        Generate final output report.
        
        Per instruction.md Section 5:
        For each page, output:
        - page_id
        - final_print_mode (B&W | Color)
        - decision_basis (metadata | pertinence_rule)
        
        Args:
            result: DocumentResult to report
            output_path: Optional path to save JSON report
            
        Returns:
            Report dictionary
        """
        report = result.to_output_dict()
        
        if output_path:
            output_path = Path(output_path)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(report, f, indent=2)
            print(f"\n📊 Report saved to: {output_path}")
        
        return report
    
    def print_detailed_report(self, result: DocumentResult):
        """
        Print human-readable detailed report to console.
        """
        print("\n" + "="*70)
        print("DETAILED PAGE-BY-PAGE REPORT")
        print("="*70)
        
        for page in result.pages:
            status_icon = "🖨️ " if page.final_print_mode == PrintMode.COLOR else "📄"
            print(f"\n{status_icon} Page {page.page_id}: {page.final_print_mode.value}")
            print(f"   Decision Source: {page.metadata_source.value if page.metadata_source else 'unknown'}")
            print(f"   Details: {page.decision_details}")
        
        print("\n" + "="*70)
        print("SUMMARY")
        print("="*70)
        print(f"Total Pages: {result.total_pages}")
        print(f"Color Pages: {len(result.get_color_pages())} - {result.get_color_pages()}")
        print(f"B&W Pages: {len(result.get_bw_pages())}")
        
        # Calculate efficiency metric
        eliminated_early = len([p for p in result.pages if p.bw_guaranteed])
        efficiency = (eliminated_early / result.total_pages * 100) if result.total_pages > 0 else 0
        print(f"\n🎯 Early Elimination Efficiency: {efficiency:.1f}%")
        print(f"   ({eliminated_early}/{result.total_pages} pages eliminated at metadata stage)")
        print("="*70)


def main():
    """
    Example usage demonstrating the pipeline.
    Per instruction.md Section 0-7 complete flow.
    """
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python main.py <pdf_path> [output_json_path]")
        print("\nExample:")
        print("  python main.py document.pdf")
        print("  python main.py document.pdf report.json")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None
    
    # Validate input
    if not Path(pdf_path).exists():
        print(f"❌ Error: File not found: {pdf_path}")
        sys.exit(1)
    
    # Create pipeline
    pipeline = ColorPrintingDecisionPipeline()
    
    # Process document
    print("\n" + "="*70)
    print("PAGE-LEVEL COLOR PRINTING DECISION SYSTEM")
    print("Per instruction.md - System-Facing, Deterministic, PRD-Aligned")
    print("="*70 + "\n")
    
    try:
        result = pipeline.process_document(pdf_path)
        
        # Print detailed report
        pipeline.print_detailed_report(result)
        
        # Generate JSON report if requested
        if output_path:
            pipeline.generate_report(result, output_path)
        
        print("\n✅ Success!")
        
    except Exception as e:
        print(f"\n❌ Error processing document: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
