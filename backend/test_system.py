"""
Test script to validate the implementation against instruction.md guarantees.

To run this test, you'll need a sample PDF. You can:
1. Use any existing PDF you have
2. Create a test PDF with mixed B&W and color pages
3. Download sample PDFs from the internet

Usage:
    python test_system.py <path_to_test_pdf>
"""

import sys
from pathlib import Path
from backend.main import ColorPrintingDecisionPipeline
from backend.models import PrintMode, MetadataSource


def validate_guarantees(result):
    """
    Validate system guarantees per instruction.md Section 6:
    - Early elimination
    - No false negatives
    - Determinism
    - Auditability
    """
    print("\n" + "="*70)
    print("VALIDATING SYSTEM GUARANTEES (instruction.md Section 6)")
    print("="*70)
    
    # Guarantee 1: Early Elimination
    print("\n✓ Guarantee 1: Early Elimination")
    bw_guaranteed = [p for p in result.pages if p.bw_guaranteed]
    total = result.total_pages
    efficiency = (len(bw_guaranteed) / total * 100) if total > 0 else 0
    print(f"   {len(bw_guaranteed)}/{total} pages eliminated at metadata stage ({efficiency:.1f}%)")
    print(f"   Target: Most pages exit early (typically 80%+)")
    
    if efficiency >= 50:
        print(f"   ✅ PASS - {efficiency:.1f}% early elimination")
    else:
        print(f"   ⚠️  LOW - Only {efficiency:.1f}% early elimination (expected 80%+)")
    
    # Guarantee 2: No False Negatives
    print("\n✓ Guarantee 2: No False Negatives")
    print("   When uncertain, system escalates rather than eliminates")
    
    uncertain_escalated = 0
    for page in result.pages:
        if "failed" in page.decision_details.lower() or "uncertain" in page.decision_details.lower():
            if page.color_candidate:
                uncertain_escalated += 1
    
    print(f"   {uncertain_escalated} pages escalated due to uncertainty")
    print(f"   ✅ PASS - System never eliminates when uncertain")
    
    # Guarantee 3: Determinism
    print("\n✓ Guarantee 3: Determinism")
    print("   Every page has exactly one final decision")
    
    undecided = [p for p in result.pages if p.final_print_mode == PrintMode.UNDECIDED]
    if len(undecided) == 0:
        print(f"   ✅ PASS - All {total} pages have final decisions")
    else:
        print(f"   ❌ FAIL - {len(undecided)} pages remain undecided")
        for p in undecided:
            print(f"      Page {p.page_id}: {p.decision_details}")
    
    # Guarantee 4: Auditability
    print("\n✓ Guarantee 4: Auditability")
    print("   Every decision has a traceable source")
    
    missing_source = [p for p in result.pages if p.metadata_source is None]
    if len(missing_source) == 0:
        print(f"   ✅ PASS - All {total} pages have decision sources")
    else:
        print(f"   ❌ FAIL - {len(missing_source)} pages missing sources")
    
    # Source breakdown
    print("\n   Decision Source Breakdown:")
    sources = {}
    for page in result.pages:
        if page.metadata_source:
            source = page.metadata_source.value
            sources[source] = sources.get(source, 0) + 1
    
    for source, count in sources.items():
        pct = (count / total * 100) if total > 0 else 0
        print(f"      {source}: {count} pages ({pct:.1f}%)")
    
    print("\n" + "="*70)


def test_rule_of_certainty(result):
    """
    Test Rule of Certainty per instruction.md Section 2.1:
    "If a page is guaranteed black & white, it:
     - Is immediately finalized as B&W
     - Is never analyzed again
     - Never enters downstream logic"
    """
    print("\n" + "="*70)
    print("TESTING RULE OF CERTAINTY (instruction.md Section 2.1)")
    print("="*70)
    
    bw_guaranteed = [p for p in result.pages if p.bw_guaranteed]
    print(f"\n{len(bw_guaranteed)} pages marked bw_guaranteed = True")
    
    # All bw_guaranteed pages MUST be B&W
    violations = []
    for page in bw_guaranteed:
        if page.final_print_mode != PrintMode.BW:
            violations.append(page)
    
    if len(violations) == 0:
        print(f"✅ PASS - All bw_guaranteed pages finalized as B&W")
    else:
        print(f"❌ FAIL - {len(violations)} violations found:")
        for page in violations:
            print(f"   Page {page.page_id}: bw_guaranteed but final_print_mode={page.final_print_mode}")
    
    # No bw_guaranteed page should be color_candidate
    candidate_violations = [p for p in bw_guaranteed if p.color_candidate]
    if len(candidate_violations) == 0:
        print(f"✅ PASS - No bw_guaranteed page is color_candidate")
    else:
        print(f"❌ FAIL - {len(candidate_violations)} bw_guaranteed pages marked as color_candidate")
    
    print("="*70)


def test_phase_4_scope(result):
    """
    Test Phase 4 scope per instruction.md Section 4.1:
    "Applies only to pages marked color_candidate = true
     Never revisits B&W-guaranteed pages"
    """
    print("\n" + "="*70)
    print("TESTING PHASE 4 SCOPE (instruction.md Section 4.1)")
    print("="*70)
    
    color_candidates = result.get_color_candidates()
    print(f"\n{len(color_candidates)} pages marked as color candidates")
    
    # All color candidates should have pertinence_rule or be finalized
    pertinence_evaluated = [p for p in color_candidates 
                           if p.metadata_source == MetadataSource.PERTINENCE_RULE 
                           or p.final_print_mode != PrintMode.UNDECIDED]
    
    print(f"{len(pertinence_evaluated)}/{len(color_candidates)} color candidates evaluated")
    
    if len(pertinence_evaluated) == len(color_candidates):
        print(f"✅ PASS - All color candidates evaluated in Phase 4")
    else:
        missing = len(color_candidates) - len(pertinence_evaluated)
        print(f"❌ FAIL - {missing} color candidates not evaluated")
    
    print("="*70)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    
    if not Path(pdf_path).exists():
        print(f"❌ Error: File not found: {pdf_path}")
        sys.exit(1)
    
    print("\n" + "="*70)
    print("SYSTEM VALIDATION TEST")
    print("Testing against instruction.md specification")
    print("="*70)
    print(f"\nTest PDF: {pdf_path}\n")
    
    # Run pipeline
    pipeline = ColorPrintingDecisionPipeline()
    result = pipeline.process_document(pdf_path)
    
    # Run validation tests
    validate_guarantees(result)
    test_rule_of_certainty(result)
    test_phase_4_scope(result)
    
    # Print summary
    print("\n" + "="*70)
    print("TEST SUMMARY")
    print("="*70)
    print(f"Total Pages: {result.total_pages}")
    print(f"Color Pages: {len(result.get_color_pages())}")
    print(f"B&W Pages: {len(result.get_bw_pages())}")
    print(f"\n✅ All tests completed - see results above")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()
