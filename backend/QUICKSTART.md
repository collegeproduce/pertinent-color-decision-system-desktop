# Quick Start Guide

## Installation

```bash
# Install dependencies
pip install -r requirements.txt
```

## Usage

### 1. Basic Usage (Console Output Only)

```bash
python main.py your_document.pdf
```

Output:
- Processing progress
- Page-by-page decisions
- Summary with efficiency metrics

### 2. Generate JSON Report

```bash
python main.py your_document.pdf output_report.json
```

Creates a JSON file with:
- All page decisions
- Decision sources (auditability)
- Color/B&W page lists

### 3. Run System Validation Tests

```bash
python test_system.py your_document.pdf
```

Validates all guarantees from instruction.md:
- Early elimination efficiency
- No false negatives
- Determinism
- Auditability

### 4. Use as Python Library

```python
from main import ColorPrintingDecisionPipeline

# Create pipeline
pipeline = ColorPrintingDecisionPipeline()

# Process document
result = pipeline.process_document("document.pdf")

# Get color pages (the main answer)
color_pages = result.get_color_pages()
print(f"Print these pages in color: {color_pages}")

# Get B&W pages
bw_pages = result.get_bw_pages()
print(f"Print these pages in B&W: {bw_pages}")

# Generate report
report = pipeline.generate_report(result, "report.json")

# Access detailed info
for page in result.pages:
    print(f"Page {page.page_id}: {page.final_print_mode.value}")
    print(f"  Source: {page.metadata_source.value}")
    print(f"  Details: {page.decision_details}")
```

## Example Outputs

### Console Output
```
====================================================================
PAGE-LEVEL COLOR PRINTING DECISION SYSTEM
Per instruction.md - System-Facing, Deterministic, PRD-Aligned
====================================================================

📄 Processing: sample.pdf
⚙️  Phase 1: Metadata Extraction (Tier 1→2→3)
   Total pages: 30
   B&W guaranteed: 27
   Color candidates: 3

⚙️  Phase 2: Document-Level Aggregation
   ⚠️  Mixed document - proceeding to pertinent color evaluation

⚙️  Phase 3: Pertinent Color Evaluation
   Evaluating 3 color candidate pages...

✅ Processing Complete
   Color pages: 1
   B&W pages: 29

🎯 Early Elimination Efficiency: 90.0%
```

### JSON Report
```json
{
  "total_pages": 30,
  "color_pages": [12],
  "bw_pages": [1, 2, 3, 4, ...],
  "page_details": [
    {
      "page_id": 1,
      "final_print_mode": "B&W",
      "decision_basis": "pdf_colorspace",
      "details": "Only DeviceGray colorspace detected"
    },
    {
      "page_id": 12,
      "final_print_mode": "Color",
      "decision_basis": "pertinence_rule",
      "details": "Color images detected (logo/chart/photo)"
    }
  ]
}
```

## Decision Flow (Per instruction.md)

```
PDF Document
    ↓
Phase 1: Metadata Extraction
    ├─ Tier 1: PDF Colorspace Check (fast)
    │   └─ DeviceGray only? → B&W ✓ STOP
    │
    ├─ Tier 2: Image Header Check
    │   └─ All images B&W? → B&W ✓ STOP
    │
    └─ Tier 3: Raster Probe (fallback)
        ├─ No color? → B&W ✓ STOP
        └─ Color found? → Escalate to Phase 4
    ↓
Phase 2: Document Aggregation
    └─ All pages B&W? → Done ✓
    └─ Mixed? → Continue
    ↓
Phase 3: Pertinent Color Evaluation (candidates only)
    ├─ Hyperlinks only? → B&W
    ├─ Logo/Chart/Image? → Color
    └─ Uncertain? → Color (no false negatives)
    ↓
Final Output: Page-by-Page Decisions
```

## Key Files

- **instruction.md** - Your north star specification
- **main.py** - Run this to process PDFs
- **test_system.py** - Run this to validate system
- **models.py** - Core data structures
- **metadata_extractor.py** - Phase 1 coordinator
- **pertinent_color_evaluator.py** - Phase 3 rules

## Troubleshooting

### Issue: "ModuleNotFoundError: No module named 'pymupdf'"
**Solution:** Run `pip install -r requirements.txt`

### Issue: Low early elimination efficiency (<50%)
**Cause:** PDF may be heavily scanned (all raster, no vector)
**Expected:** Tier 3 handles this correctly

### Issue: Page marked B&W but should be Color
**Action:** Check detailed report for decision source
**Verify:** Is color truly pertinent per instruction.md rules?

### Issue: Want to customize pertinent color rules
**File:** Edit `pertinent_color_evaluator.py`
**Rules:** Add new deterministic checks (no ML)

## System Guarantees (Always True)

✅ **Early Elimination** - Most pages stop at metadata (no heavy processing)  
✅ **No False Negatives** - Never eliminates meaningful color  
✅ **Deterministic** - Same PDF → same results  
✅ **Auditable** - Every decision has a source  

## Next Steps

1. Install dependencies: `pip install -r requirements.txt`
2. Test with a sample PDF: `python main.py test.pdf`
3. Run validation tests: `python test_system.py test.pdf`
4. Review [instruction.md](instruction.md) for detailed specification
5. Customize pertinent color rules if needed
