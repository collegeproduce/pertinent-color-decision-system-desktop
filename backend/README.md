# Page-Level Color Printing Decision System

**System-Facing, Deterministic, PRD-Aligned**

Implementation of the pertinent color detection system per [instruction.md](instruction.md).

## System Intent

Determine which pages of a PDF document must be printed in color - nothing else.
++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++d
## Core Principle

> A page is black & white unless it proves it can be color, and even then, it must prove that color matters.

## Architecture

### 3-Tier Metadata Extraction (Progressive Elimination)

1. **Tier 1: PDF Colorspace Inspection** (kills 60-75% instantly)
   - Inspects page content streams for colorspace operators
   - No rasterization - pure parsing
   - DeviceGray only → B&W guaranteed

2. **Tier 2: Image Header Inspection** (kills 10-15% more)
   - Reads embedded image dictionaries
   - No pixel decoding
   - All images grayscale → B&W guaranteed

3. **Tier 3: Low-Cost Raster Probe** (handles edge cases)
   - Renders at low DPI (72)
   - Samples pixels efficiently
   - No color detected → B&W guaranteed
   - Color detected → escalate to Phase 4

### Phase 4: Pertinent Color Evaluation

Only for pages that survived Tier 1-3:

- **Deterministic rule-based logic** (no ML)
- Hyperlink-only color → B&W
- Logos, charts, images, highlights → Color

## Installation

```bash
pip install pymupdf Pillow
```

## Usage

### Basic Command Line

```bash
python main.py document.pdf
```

### With JSON Report Output

```bash
python main.py document.pdf report.json
```

### As a Library

```python
from main import ColorPrintingDecisionPipeline

pipeline = ColorPrintingDecisionPipeline()
result = pipeline.process_document("document.pdf")

# Get color pages
color_pages = result.get_color_pages()
print(f"Print in color: {color_pages}")

# Get full report
report = pipeline.generate_report(result)
```

## Project Structure

```
pertinentcolors/
├── instruction.md              # Execution flow specification
├── models.py                   # Core data structures
├── tier1_tier2.py             # Tier 1 & 2 metadata extractors
├── tier3.py                   # Tier 3 raster probe
├── metadata_extractor.py      # Metadata extraction orchestrator
├── pertinent_color_evaluator.py  # Pertinent color rules
└── main.py                    # Main pipeline & CLI
```

## System Guarantees

Per [instruction.md](instruction.md) Section 6:

✅ **Early Elimination** - Most pages exit at metadata stage  
✅ **No False Negatives** - When uncertain, escalate (never eliminate color that matters)  
✅ **Determinism** - Same input → same output  
✅ **Auditability** - Every decision has a traceable source  

## Output Format

```json
{
  "total_pages": 30,
  "color_pages": [12, 21],
  "bw_pages": [1, 2, 3, ...],
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
      "details": "Pertinent color detected: Color images (logo/chart/photo)"
    }
  ]
}
```

## Development Notes

- **PyInstaller-ready** - No heavy dependencies, offline-capable
- **No ML/AI** - Pure deterministic logic
- **Follows instruction.md** - Every component maps to specification
- **Efficient** - Progressive elimination, early exits

## Example Output

```
====================================================================
PAGE-LEVEL COLOR PRINTING DECISION SYSTEM
Per instruction.md - System-Facing, Deterministic, PRD-Aligned
====================================================================

📄 Processing: document.pdf
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

====================================================================
SUMMARY
====================================================================
Total Pages: 30
Color Pages: 1 - [21]
B&W Pages: 29

🎯 Early Elimination Efficiency: 90.0%
   (27/30 pages eliminated at metadata stage)
====================================================================
```

## License

Implementation aligned with instruction.md specification.
