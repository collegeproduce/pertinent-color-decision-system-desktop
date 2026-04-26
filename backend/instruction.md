EXECUTION FLOW
Page-Level Color Printing Decision System

(System-Facing, Deterministic, PRD-Aligned)

0. SYSTEM INTENT (NON-NEGOTIABLE)

The system exists for one purpose only:

Determine which pages of an uploaded PDF must be printed in color.

The system does not:

Assess aesthetics

Classify documents

Perform OCR by default

Use machine learning

Make probabilistic judgments

All logic exists solely to support this decision.

1. INPUT & INITIALIZATION
1.1 Input

One or more PDF documents uploaded by the user

Example:

1 PDF

30 pages

1.2 Initialize Page Ledger

For each page in the PDF, initialize a page record:

page_id
bw_guaranteed = unknown
color_candidate = unknown
final_print_mode = undecided
metadata_source = null


Each page is treated as independent.

2. PAGE-LEVEL METADATA EXTRACTION (EARLY ELIMINATION)

This phase exists to eliminate pages that can never be printed in color.

2.1 Rule of Certainty

If a page is guaranteed black & white, it:

Is immediately finalized as B&W

Is never analyzed again

Never enters downstream logic

No exceptions. No overrides.

2.2 Tier 1: PDF Structural Colorspace Inspection

For each page:

Inspect the page’s content stream

Identify all colorspaces invoked

Decision Rules

If only DeviceGray is used:

bw_guaranteed = true

final_print_mode = B&W

Record metadata_source = pdf_colorspace

STOP processing this page

If any non-gray colorspace is used:

Proceed to Tier 2

2.3 Tier 2: Embedded Image Metadata Inspection

Only applies if Tier 1 does not guarantee B&W.

For each image on the page:

Read image dictionary only (no pixel decoding)

Decision Rules

If all images are:

DeviceGray

1-bit images

Image masks

→ bw_guaranteed = true
→ final_print_mode = B&W
→ Record metadata_source = image_header
→ STOP processing this page

If any image is RGB / CMYK:

Proceed to Tier 3

2.4 Tier 3: Low-Cost Raster Color Probe (Fallback)

Only applies if Tier 1 and Tier 2 are inconclusive.

Render page at very low DPI (≈ 50–72)

Sample a small subset of pixels

Check for non-grayscale values beyond tolerance

Decision Rules

If no color detected:

bw_guaranteed = true

final_print_mode = B&W

Record metadata_source = raster_probe

STOP processing this page

If any color detected:

color_candidate = true

Record metadata_source = raster_probe

Proceed to downstream evaluation

3. DOCUMENT-LEVEL AGGREGATION (OPTIMIZATION ONLY)

After all pages are processed through metadata extraction:

If all pages are bw_guaranteed = true:

Finalize entire document as B&W

End processing

If any page is color_candidate = true:

Document is considered mixed

Proceed to pertinent color evaluation for those pages only

This step never changes page-level decisions.

4. PERTINENT COLOR EVALUATION

(ONLY for color candidates)

This phase answers the business question.

Does the detected color justify printing this page in color?

4.1 Scope Limitation

This phase:

Applies only to pages marked color_candidate = true

Never revisits B&W-guaranteed pages

Uses deterministic, rule-based logic only

4.2 Evaluation Rules (Examples, Not Exhaustive)

Each candidate page is evaluated independently.

Example: Hyperlink-Only Color

If color on the page:

Is blue

Underlined

Inline with text

Matches URL patterns

→ Color is non-pertinent
→ final_print_mode = B&W

Example: Pertinent Color

If color is associated with:

Logos

Charts

Highlights

Seals / stamps

Images conveying information

→ Color is pertinent
→ final_print_mode = Color

5. FINAL OUTPUT

For each page, output:

page_id
final_print_mode (B&W | Color)
decision_basis (metadata | pertinence_rule)


Only pages explicitly marked Color are printed in color.
All others default to B&W.

6. SYSTEM GUARANTEES

The system guarantees:

Early elimination

Most pages exit at metadata stage

No false negatives

If uncertain, escalate

Determinism

Same input → same output

Auditability

Every decision has a source

7. ONE-LINE SYSTEM TRUTH

A page is black & white unless it proves it can be color, and even then, it must prove that color matters.