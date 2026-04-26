"""
CPCE - Legal Arbitration Engine

Resolves conflicts between signals using legal evidence hierarchy, then
performs a document-level confidence normalization pass after all pages
are scored.

Why this exists
---------------
The pertinence engine, BERT refinement, and visual-truth law produce a
per-page score that is numerically sound but legally blind: a stamp at
density 0.012 and a color annotation at density 0.012 are treated as
identical signals.  A paralegal knows they are not.

This module adds two capabilities the previous layers lack:

  1. Legal authority hierarchy
     Each dominant_factor is mapped to an authority weight [0, 1] that
     reflects how self-evidently probative that evidence type is.  A
     photo (0.88) commands a decision without semantic corroboration.
     A cross-page-visual signal (0.35) merely nudges one.

  2. Conflict resolution
     When _detect_signal_conflicts() returns a non-"none" type, the
     current engine only subtracts a confidence penalty.  It does NOT
     decide which signal to trust.  This module does:

       - visual_unsupported  → on evidence pages  trust visual (photo IS evidence)
                            → on text pages       discount visual, weight semantic more
       - ref_without_content → cap score below COLOR threshold (spurious link)
       - pertinence_semantic_gap → if directive/VIG explains the gap, accept it;
                                   otherwise discount pertinence slightly

  3. Document-level confidence normalization
     After all pages are scored, recalibrate REVIEW_REQUIRED pages using
     z-scores relative to the document-wide score distribution.

     A page at 0.45 in a COLOR-heavy document (mean 0.65) is relatively
     weak → lower its confidence slightly.
     A page at 0.45 in a B/W document (mean 0.12) is actually a clear
     COLOR candidate → raise its confidence.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Dict, List, Tuple


# ─────────────────────────────────────────────────────────────
# Legal Authority Hierarchy
# ─────────────────────────────────────────────────────────────
# Each entry: (signal_name, authority_weight)
# Ordered highest → lowest legal authority.
# authority_weight reflects how self-evidently probative the signal is —
# i.e., whether it can command a COLOR decision without corroboration.

LEGAL_HIERARCHY: List[Tuple[str, float]] = [
    ("exhibit_stamp",              0.95),  # Certified exhibit = highest authority
    ("signature",                  0.90),  # Contract execution proof
    ("photo_evidence",             0.88),  # Photographic evidence = self-evidentiary
    ("page_role:medical_image",    0.88),  # Medical scan = diagnostic necessity
    ("page_role:evidence_photo",   0.85),  # Evidence photograph
    ("chart_evidence",             0.80),  # Data visualization (financial/technical)
    ("page_role:exhibit_page",     0.78),  # Exhibit body page
    ("page_role:financial_chart",  0.75),  # Financial chart page
    ("highlight",                  0.72),  # Attorney annotation = intentional emphasis
    ("page_role:signature_page",   0.72),  # Signature / notary page
    ("colored_annotation",         0.68),  # Red/orange attorney markup
    ("stamp",                      0.65),  # Official stamp or seal
    ("exhibit_resolution",         0.62),  # Page resolving a referenced exhibit
    ("cross_reference",            0.60),  # Explicit cross-page exhibit reference
    ("visual_attr_confirmed",      0.58),  # Instruction + confirmed visual attribute
    ("directive",                  0.55),  # Legal instruction ("see following page")
    ("cross_page_context",         0.50),  # Cross-page context from prior directives
    ("semantic_match",             0.40),  # Legal keyword relevance
    ("cross_page_visual",          0.35),  # Visual instruction propagation (VIG)
    ("none",                       0.10),  # No dominant signal
]

_AUTHORITY_WEIGHT: Dict[str, float] = {name: w for name, w in LEGAL_HIERARCHY}

# Signals whose authority is modulated by confirmed visual evidence quality.
# Non-grounded signals (directive, semantic_match, etc.) are unaffected.
_VISUALLY_GROUNDED_SIGNALS: frozenset = frozenset({
    "photo_evidence",
    "chart_evidence",
    "stamp",
    "highlight",
    "colored_annotation",
    "page_role:medical_image",
    "page_role:evidence_photo",
    "page_role:financial_chart",
    "page_role:exhibit_page",
})

# Per-case authority multipliers.  Keys match CaseTypeDetector output strings.
# Only signals explicitly listed are scaled; all others are unchanged.
CASE_AUTHORITY_SCALE: Dict[str, Dict[str, float]] = {
    "contract_dispute": {"signature": 1.0, "stamp": 1.10, "photo_evidence": 0.90, "highlight": 0.95},
    "personal_injury":  {"photo_evidence": 1.10, "chart_evidence": 1.05, "stamp": 0.90},
    "medical":          {"photo_evidence": 1.10, "chart_evidence": 1.05},
    "criminal":         {"photo_evidence": 1.10, "highlight": 1.05, "stamp": 1.05},
    "insurance":        {"photo_evidence": 1.10, "chart_evidence": 1.05},
    "ip":               {"chart_evidence": 1.10, "photo_evidence": 1.05},
    "real_estate":      {"photo_evidence": 1.08, "chart_evidence": 1.05},
}


def _get_authority_weight(factor: str) -> float:
    """Return legal authority weight for a dominant_factor string."""
    if factor in _AUTHORITY_WEIGHT:
        return _AUTHORITY_WEIGHT[factor]
    if factor.startswith("page_role:"):
        return _AUTHORITY_WEIGHT.get(factor, 0.35)
    return 0.20


_PRIORITY_LABELS: Dict[str, str] = {
    "exhibit_stamp":           "CERTIFIED EXHIBIT",
    "signature":               "SIGNATURE/EXECUTION",
    "photo_evidence":          "PHOTOGRAPHIC EVIDENCE",
    "chart_evidence":          "DATA VISUALIZATION",
    "highlight":               "ATTORNEY ANNOTATION",
    "colored_annotation":      "ATTORNEY MARKUP",
    "stamp":                   "OFFICIAL STAMP/SEAL",
    "exhibit_resolution":      "EXHIBIT BODY",
    "cross_reference":         "CROSS-PAGE REFERENCE",
    "visual_attr_confirmed":   "CONFIRMED VISUAL ATTR",
    "directive":               "LEGAL DIRECTIVE",
    "cross_page_context":      "CROSS-PAGE CONTEXT",
    "semantic_match":          "SEMANTIC RELEVANCE",
    "cross_page_visual":       "VISUAL PROPAGATION",
    "none":                    "NO DOMINANT SIGNAL",
}


def _priority_label(factor: str) -> str:
    if factor in _PRIORITY_LABELS:
        return _PRIORITY_LABELS[factor]
    if factor.startswith("page_role:"):
        role = factor.split(":", 1)[1].upper().replace("_", " ")
        return f"PAGE ROLE: {role}"
    return f"SIGNAL: {factor}"


# ─────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────

@dataclass
class ArbitrationResult:
    """Output of the legal arbitration engine for one page."""
    arbitrated_score:    float  # final score after arbitration [0, 1]
    authority_weight:    float  # legal authority weight of dominant signal
    priority_level:      str    # human-readable legal priority label
    conflict_resolved:   bool   # True if a conflict was actively resolved
    conflict_resolution: str    # description of resolution (empty if none)
    justification:       str    # 1-2 sentence compressed justification


# ─────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────

class LegalArbitrationEngine:
    """
    Per-page signal arbitration + document-level confidence normalization.

    Per-page: call arbitrate() after effective_pertinence is computed and
    conflicts are detected, but before _make_decision_v10().

    Document-level: call normalize_document_confidence(results) after all
    pages are processed (after BERT refinement and score propagation).
    """

    # When authority_weight >= this, the signal commands COLOR without
    # requiring semantic corroboration.
    AUTHORITY_COMMAND_THRESHOLD: float = 0.80

    # How much stronger directive must be relative to visual_score to
    # take precedence in the directive-vs-visual resolution path.
    DIRECTIVE_DOMINANCE_RATIO: float = 1.30

    # Z-score threshold for document normalization to upgrade/downgrade
    # REVIEW_REQUIRED pages.
    NORM_Z_THRESHOLD: float = 0.80

    # ── Per-page arbitration ─────────────────────────────────────────

    def arbitrate(
        self,
        dominant_factor:          str,
        visual_score:             float,
        semantic_score:           float,
        effective_pertinence:     float,
        visual_propagation_score: float = 0.0,
        prior_directive_count:    int   = 0,
        ref_strength:             float = 0.0,
        conflict_type:            str   = "none",
        page_role:                str   = "unknown",
        case_type:                str   = "general_litigation",
        visual_evidence_score:    float = 0.0,
    ) -> ArbitrationResult:
        """
        Apply legal hierarchy + conflict resolution.

        Steps:
          1. Map dominant_factor → authority_weight
          2. High-authority command: floor score to COLOR zone
          3. Conflict resolution: choose which signal to trust
          4. Directive precedence: directive > weak visual
          5. Build compressed justification
        """
        auth_w       = _get_authority_weight(dominant_factor)
        p_label      = _priority_label(dominant_factor)
        score        = effective_pertinence
        resolved     = False
        resolution   = ""

        # ── 0a. Case-type authority scaling ───────────────────────────
        # Applied first (before VE scaling) so case context adjusts the
        # base authority weight, and VE scaling then modulates the result.
        _case_scale = CASE_AUTHORITY_SCALE.get(case_type, {})
        if dominant_factor in _case_scale:
            auth_w = round(min(1.0, auth_w * _case_scale[dominant_factor]), 4)

        # ── 0b. Visual-evidence (VE) authority scaling ─────────────────
        # Visually-grounded signals scale by confirmed visual evidence
        # quality so a corner logo and a full-page injury photo are not
        # treated identically.
        # formula: effective = base × (0.50 + min(0.50, ve_score))
        #   ve=0.0 → 50 % authority  (no visual confirmation)
        #   ve=1.0 → 100 % authority (fully confirmed visual)
        if dominant_factor in _VISUALLY_GROUNDED_SIGNALS:
            ve_factor = 0.50 + min(0.50, visual_evidence_score)
            auth_w = round(auth_w * ve_factor, 4)

        # ── 1. High-authority command ──────────────────────────────────
        # Self-evidently probative signals (photos, stamps, signatures)
        # do not require semantic corroboration to reach COLOR.
        if auth_w >= self.AUTHORITY_COMMAND_THRESHOLD:
            if score < 0.76:
                score = round(max(score, 0.70 + auth_w * 0.08), 4)
                resolved   = True
                resolution = (
                    f"High-authority signal [{p_label}] (weight {auth_w:.2f}) "
                    "commands COLOR — semantic corroboration not required"
                )

        # ── 2. Conflict resolution ─────────────────────────────────────
        elif conflict_type != "none":
            score, resolved, resolution = self._resolve_conflict(
                conflict_type=conflict_type,
                auth_w=auth_w,
                visual_score=visual_score,
                semantic_score=semantic_score,
                effective_pertinence=effective_pertinence,
                prior_directive_count=prior_directive_count,
                ref_strength=ref_strength,
                visual_propagation_score=visual_propagation_score,
                page_role=page_role,
            )

        # ── 3. Directive precedence (independent of conflict_type) ─────
        # "See red text on the following page" is a legal instruction.
        # When visual is weak but multiple directives point forward,
        # the directive score takes precedence.
        elif prior_directive_count > 0 and visual_score < 0.30:
            directive_score = min(0.65, prior_directive_count * 0.20 + ref_strength * 0.30)
            if directive_score > visual_score * self.DIRECTIVE_DOMINANCE_RATIO:
                score    = round(max(score, directive_score), 4)
                resolved = True
                resolution = (
                    f"Directive precedence: {prior_directive_count} forward directive(s) "
                    f"override weak visual (directive={directive_score:.2f} > "
                    f"visual={visual_score:.2f} × {self.DIRECTIVE_DOMINANCE_RATIO})"
                )

        score = round(min(1.0, max(0.0, score)), 4)

        zone = (
            "COLOR" if score >= 0.75
            else ("B/W" if score <= 0.25 else "REVIEW")
        )
        if auth_w >= self.AUTHORITY_COMMAND_THRESHOLD:
            justification = (
                f"[{p_label}] Self-authorizing evidence — decision: {zone}."
            )
        elif resolved:
            short = resolution.split(" — ")[0] if " — " in resolution else resolution
            justification = f"[{p_label}] {short[:120]} — decision: {zone}."
        else:
            justification = (
                f"[{p_label}] Score {score:.3f} (authority {auth_w:.2f}) — decision: {zone}."
            )

        return ArbitrationResult(
            arbitrated_score=score,
            authority_weight=auth_w,
            priority_level=p_label,
            conflict_resolved=resolved,
            conflict_resolution=resolution,
            justification=justification,
        )

    # ── Document-level normalization ─────────────────────────────────

    def normalize_document_confidence(self, results: list) -> None:
        """
        Post-processing: recalibrate REVIEW_REQUIRED pages relative to the
        document-wide score distribution.

        Only REVIEW_REQUIRED pages are eligible — COLOR and B/W pages are
        already settled.

        Algorithm:
          doc_mean = mean(final_score) across all pages
          doc_std  = stdev(final_score) (floor 0.05)
          z = (page.final_score - doc_mean) / doc_std

          z >= +NORM_Z_THRESHOLD AND doc_mean >= 0.50 AND confidence >= 0.55
            → confidence += 0.10  (above-average in a COLOR-leaning doc)

          z <= -NORM_Z_THRESHOLD AND doc_mean <= 0.45 AND confidence < 0.55
            → confidence -= 0.08  (below-average in a B/W-leaning doc)
        """
        if not results:
            return

        scores = [r.final_score for r in results]
        if len(scores) < 2:
            return

        doc_mean = statistics.mean(scores)
        doc_std  = statistics.stdev(scores) if len(scores) > 2 else 0.10
        if doc_std < 0.05:
            doc_std = 0.05

        for r in results:
            if not r.is_review_required:
                continue

            z = (r.final_score - doc_mean) / doc_std

            if z >= self.NORM_Z_THRESHOLD and doc_mean >= 0.50 and r.confidence >= 0.55:
                r.confidence = round(min(1.0, r.confidence + 0.10), 4)
                if r.reasoning_trace is None:
                    r.reasoning_trace = []
                r.reasoning_trace.append(
                    f"Arbitration/Normalization: z={z:.2f}, doc_mean={doc_mean:.3f} "
                    f"(COLOR-leaning) — page is above-average; "
                    f"confidence raised to {r.confidence:.2f}"
                )

            elif z <= -self.NORM_Z_THRESHOLD and doc_mean <= 0.45 and r.confidence < 0.55:
                r.confidence = round(max(0.0, r.confidence - 0.08), 4)
                if r.reasoning_trace is None:
                    r.reasoning_trace = []
                r.reasoning_trace.append(
                    f"Arbitration/Normalization: z={z:.2f}, doc_mean={doc_mean:.3f} "
                    f"(B/W-leaning) — page is below-average; "
                    f"confidence lowered to {r.confidence:.2f}"
                )

    # ── Internal: conflict resolution ────────────────────────────────

    def _resolve_conflict(
        self,
        conflict_type:            str,
        auth_w:                   float,
        visual_score:             float,
        semantic_score:           float,
        effective_pertinence:     float,
        prior_directive_count:    int,
        ref_strength:             float,
        visual_propagation_score: float,
        page_role:                str,
    ) -> Tuple[float, bool, str]:
        """
        Choose which signal to trust for each known conflict type.
        Returns (resolved_score, conflict_resolved, resolution_description).
        """
        score    = effective_pertinence
        resolved = False
        desc     = ""

        if conflict_type == "visual_unsupported":
            # Strong visual, weak semantic.
            # Evidence pages: photos ARE the evidence — semantic weakness is expected.
            # Text pages: semantic weakness undermines unexplained visual signal.
            if page_role in ("evidence_photo", "medical_image"):
                score    = round(max(score, visual_score * 0.90), 4)
                resolved = True
                desc     = (
                    f"Evidence page — visual ({visual_score:.2f}) trusted as self-evidentiary "
                    f"despite weak semantic ({semantic_score:.2f})"
                )
            else:
                # Blend conservatively toward semantic to penalize unsupported visual
                score    = round(0.60 * effective_pertinence + 0.40 * semantic_score, 4)
                resolved = True
                desc     = (
                    f"Text page — visual ({visual_score:.2f}) discounted; "
                    f"semantic ({semantic_score:.2f}) weighted more heavily"
                )

        elif conflict_type == "ref_without_content":
            # High pertinence from references, but page content is empty.
            # Possible OCR failure, but we cannot confirm content that we cannot see.
            # Cap below COLOR threshold to prevent false COLOR decisions.
            score    = round(min(score, 0.60), 4)
            resolved = True
            desc     = (
                "High reference score without supporting content — "
                "score capped at 0.60 to prevent false COLOR decision"
            )

        elif conflict_type == "high_refs_low_content":
            if ref_strength >= 0.70:
                # Strong exhibit reference is itself legal authority — accept it
                resolved = True
                desc     = (
                    f"Strong exhibit reference (strength={ref_strength:.2f}) "
                    "accepted as authority despite sparse content"
                )
            else:
                score    = round(min(score, 0.65), 4)
                resolved = True
                desc     = (
                    f"Moderate references with sparse content — "
                    f"score capped at 0.65 (ref_strength={ref_strength:.2f})"
                )

        elif conflict_type == "pertinence_semantic_gap":
            # Elevated pertinence (from refs/graph) but near-zero semantic.
            # Directive-driven or VIG-driven pages legitimately show this pattern —
            # the gap is explained by cross-page logic, not missing content.
            if prior_directive_count > 0 or visual_propagation_score >= 0.20:
                resolved = True
                desc     = (
                    f"Semantic gap explained by cross-page signal "
                    f"(directives={prior_directive_count}, "
                    f"VIG={visual_propagation_score:.2f}) — pertinence retained"
                )
            else:
                score    = round(score * 0.85, 4)
                resolved = True
                desc     = (
                    f"Pertinence-semantic gap without cross-page context — "
                    f"score discounted {effective_pertinence:.3f} → {score:.3f}"
                )

        return score, resolved, desc
