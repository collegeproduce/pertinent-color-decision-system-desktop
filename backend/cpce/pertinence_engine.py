"""
CPCE v8 - Color Pertinence Engine
The core legal question: "Does this color matter to the case, or is it just decorative?"

This module provides a dedicated scoring axis (pertinence_score) that becomes
the primary decision driver, replacing the raw weighted sum.
"""
import math
import re
from typing import List, Dict, Tuple, Any
from dataclasses import dataclass, field

from .models import VisualFeatures, SemanticFeatures


def _soft_scale(raw: float) -> float:
    """
    Compress raw score into a graded [0.05, 0.95] range using tanh.
    Prevents the extreme 0.07 vs 1.0 binary behaviour — scores remain
    meaningfully separated but no page jumps straight to floor or ceiling.

    Mapping examples:
      raw=0.10 → ~0.22   raw=0.30 → ~0.43
      raw=0.50 → ~0.62   raw=0.70 → ~0.79
      raw=0.90 → ~0.91
    """
    # tanh centred at 0.4 with stretch 3.5 gives a wide usable range
    t = math.tanh((raw - 0.40) * 3.5)
    return round(max(0.05, min(0.95, 0.5 + 0.45 * t)), 4)


# ─────────────────────────────────────────────────────────────
# Legal Knowledge Ontology
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# Universal Legal Instruction Detector
# ─────────────────────────────────────────────────────────────
# Intent-structure approach: a directive is present when an instruction
# verb co-occurs with a reference direction OR a visual attribute.
# This captures loosely-worded phrases that exact-match regex misses:
#   "see this below in red"         ✓  (instruction + reference + visual)
#   "refer to the attached diagram" ✓  (instruction + visual)
#   "note the highlighted section"  ✓  (instruction + visual)

_INSTRUCTION_VERBS: frozenset = frozenset([
    "see", "refer", "check", "review", "examine", "inspect",
    "consider", "note", "observe", "consult", "view",
])
_REFERENCE_WORDS: frozenset = frozenset([
    "below", "above", "following", "attached", "herein", "hereto",
    "next", "subsequent", "previous", "appended", "enclosed",
])
# Visual attribute keywords — split by type for confirmation logic
_COLOR_ATTR_KEYWORDS: frozenset = frozenset([
    "red", "blue", "green", "yellow", "orange", "purple", "pink",
    "highlighted", "highlight", "marked", "circled", "underlined", "bold",
    "color", "colour",
])
_VISUAL_OBJ_KEYWORDS: frozenset = frozenset([
    "image", "photo", "photograph", "picture", "figure", "diagram",
    "chart", "graph", "exhibit", "document", "illustration", "drawing",
])
_VISUAL_ATTR_KEYWORDS: frozenset = _COLOR_ATTR_KEYWORDS | _VISUAL_OBJ_KEYWORDS


def detect_legal_instruction_universal(text: str) -> bool:
    """
    Return True when the text contains a forward-looking legal directive,
    regardless of exact phrasing.

    Rule: instruction_verb AND (reference_word OR visual_attribute)

    Works sentence-by-sentence so a multi-sentence page isn't treated
    as one giant clause.
    """
    text_lower = text.lower()
    for sent in re.split(r'[.\n;]', text_lower):
        words = set(re.findall(r'\b\w+\b', sent))
        if words & _INSTRUCTION_VERBS:
            if (words & _REFERENCE_WORDS) or (words & _VISUAL_ATTR_KEYWORDS):
                return True
    return False


def _visual_attr_confirmed(text_lower: str, visual: 'VisualFeatures') -> Tuple[bool, str]:
    """
    Cross-reference visual attribute keywords in text with actual visual features.

    Returns (confirmed, matched_attr) where confirmed is True when the text
    mentions an attribute AND the visual data supports it:
      • "red/circled/marked"  → stamp_density > 0 OR highlight_density > 0
      • "highlighted/yellow"  → highlight_density > 0
      • "image/photo/picture" → photo_regions > 0 OR grayscale_regions > 0
      • "chart/graph/diagram" → chart_regions > 0
      • "color/colour"        → color_density > 0.02

    This links textual intent to visual evidence — the missing step between
    "detect the instruction" and "confirm the content exists".
    """
    words = set(re.findall(r'\b\w+\b', text_lower))

    # Color highlights
    if words & {"highlighted", "highlight", "yellow", "green", "pink"}:
        if getattr(visual, 'highlight_density', 0) > 0.001:
            return True, "highlighted_text_confirmed"

    # Red/circled/marked stamps or highlights
    if words & {"red", "circled", "marked", "blue", "stamped"}:
        if (getattr(visual, 'stamp_density', 0) > 0.0005
                or getattr(visual, 'highlight_density', 0) > 0.001):
            return True, "color_mark_confirmed"

    # Photographic content
    if words & {"image", "photo", "photograph", "picture"}:
        if (getattr(visual, 'photo_regions', 0) > 0
                or getattr(visual, 'grayscale_regions', 0) > 0):
            return True, "photo_content_confirmed"

    # Charts/diagrams
    if words & {"chart", "graph", "diagram", "figure", "illustration"}:
        if getattr(visual, 'chart_regions', 0) > 0:
            return True, "chart_content_confirmed"

    # Generic color mention
    if words & {"color", "colour"}:
        if getattr(visual, 'color_density', 0) > 0.02:
            return True, "color_presence_confirmed"

    return False, ""


# Phrases that imply color is legally referenced (not just decorative)
EVIDENCE_CITATION_PHRASES = [
    "as shown in", "as depicted", "as illustrated", "as displayed",
    "see attached", "see exhibit", "refer to exhibit", "attached hereto",
    "photograph shows", "image depicts", "photo demonstrates",
    "color indicates", "highlighted in", "marked in red", "circled in",
    "shown in blue", "green indicates", "red arrow", "blue outline",
    "as shown on page", "see figure", "refer to figure",
]

MEDICAL_EVIDENCE_PHRASES = [
    "x-ray", "x ray", "xray", "mri", "ct scan", "ct-scan", "radiograph",
    "ultrasound", "pathology", "biopsy", "scan shows", "imaging reveals",
    "diagnostic image", "medical image", "clinical photograph",
    "wound photograph", "injury photograph", "surgical photograph",
]

FINANCIAL_EVIDENCE_PHRASES = [
    "graph shows", "chart indicates", "trend line", "as illustrated in figure",
    "revenue breakdown", "profit margin", "loss chart", "financial graph",
    "data visualization", "pie chart", "bar graph", "line graph",
]

# v16: Signature and authentication phrases (critical in contract cases)
SIGNATURE_AUTH_PHRASES = [
    "signature", "signed by", "signatory", "notarized", "notary public",
    "witness signature", "executed by", "authorized signature", "seal of",
    "corporate seal", "official seal", "sworn and subscribed", "sworn before",
    "acknowledged before", "under penalty of perjury", "subscribed and sworn",
    "in witness whereof", "electronic signature", "wet signature",
]

# v16: Technical diagram phrases (critical in IP / engineering cases)
TECHNICAL_DIAGRAM_PHRASES = [
    "technical drawing", "engineering drawing", "schematic", "blueprint",
    "figure shows", "diagram illustrates", "design drawing", "patent drawing",
    "isometric view", "cross-section", "cross section", "technical specification",
    "claim chart", "prior art", "embodiment", "as shown in figure",
    "figure 1", "figure 2", "fig.", "technical diagram", "block diagram",
]

# Default page role → legal relevance weight (general litigation)
PAGE_ROLE_WEIGHTS: Dict[str, float] = {
    "medical_image":   1.00,
    "evidence_photo":  0.95,
    "exhibit_page":    0.90,
    "financial_chart": 0.80,
    "signature_page":  0.60,
    "correspondence":  0.20,
    "legal_argument":  0.10,
    "boilerplate_text": 0.05,
    "unknown":         0.30,
}

# v16: Case-specific page role weights — overrides PAGE_ROLE_WEIGHTS per case type
_CASE_ROLE_WEIGHTS: Dict[str, Dict[str, float]] = {
    "personal_injury": {
        "medical_image":    1.00,
        "evidence_photo":   0.98,  # injury photos CRITICAL
        "exhibit_page":     0.88,
        "financial_chart":  0.48,  # charts secondary in PI
        "signature_page":   0.52,
        "correspondence":   0.18,
        "legal_argument":   0.08,
        "boilerplate_text": 0.05,
        "unknown":          0.28,
    },
    "medical": {
        "medical_image":    1.00,
        "evidence_photo":   0.98,
        "exhibit_page":     0.85,
        "financial_chart":  0.42,
        "signature_page":   0.38,
        "correspondence":   0.15,
        "legal_argument":   0.05,
        "boilerplate_text": 0.05,
        "unknown":          0.25,
    },
    "contract_dispute": {
        "medical_image":    0.32,  # medical imagery rarely relevant
        "evidence_photo":   0.42,
        "exhibit_page":     0.90,
        "financial_chart":  0.88,  # financial data often central
        "signature_page":   0.98,  # CRITICAL — signatures ARE the evidence
        "correspondence":   0.28,
        "legal_argument":   0.15,
        "boilerplate_text": 0.06,
        "unknown":          0.30,
    },
    "ip": {
        "medical_image":    0.20,
        "evidence_photo":   0.58,  # product comparison photos
        "exhibit_page":     0.88,
        "financial_chart":  0.90,  # technical / market charts CRITICAL
        "signature_page":   0.52,
        "correspondence":   0.18,
        "legal_argument":   0.12,
        "boilerplate_text": 0.05,
        "unknown":          0.30,
    },
    "real_estate": {
        "medical_image":    0.20,
        "evidence_photo":   0.92,  # property photos CRITICAL
        "exhibit_page":     0.90,
        "financial_chart":  0.84,  # survey, appraisal charts
        "signature_page":   0.62,
        "correspondence":   0.22,
        "legal_argument":   0.10,
        "boilerplate_text": 0.05,
        "unknown":          0.30,
    },
    "criminal": {
        "medical_image":    0.88,  # autopsy / injury documentation
        "evidence_photo":   0.98,  # crime scene CRITICAL
        "exhibit_page":     0.92,
        "financial_chart":  0.48,
        "signature_page":   0.48,
        "correspondence":   0.15,
        "legal_argument":   0.08,
        "boilerplate_text": 0.05,
        "unknown":          0.28,
    },
    "insurance": {
        "medical_image":    0.90,
        "evidence_photo":   0.95,  # damage photos CRITICAL
        "exhibit_page":     0.88,
        "financial_chart":  0.80,  # loss valuation charts
        "signature_page":   0.52,
        "correspondence":   0.18,
        "legal_argument":   0.08,
        "boilerplate_text": 0.05,
        "unknown":          0.28,
    },
    "evidence_hearing": {
        "medical_image":    0.90,
        "evidence_photo":   0.97,
        "exhibit_page":     0.92,
        "financial_chart":  0.72,
        "signature_page":   0.60,
        "correspondence":   0.18,
        "legal_argument":   0.10,
        "boilerplate_text": 0.05,
        "unknown":          0.30,
    },
}

# v16: How relevant each phrase category is to each case type (multiplier on base score)
_CASE_PHRASE_MULTIPLIERS: Dict[str, Dict[str, float]] = {
    "personal_injury": {
        "medical": 1.05, "evidence_citation": 1.00, "financial": 0.72,
        "signature_auth": 0.68, "technical_diagram": 0.48,
    },
    "medical": {
        "medical": 1.05, "evidence_citation": 0.90, "financial": 0.58,
        "signature_auth": 0.48, "technical_diagram": 0.38,
    },
    "contract_dispute": {
        "medical": 0.48, "evidence_citation": 0.80, "financial": 0.95,
        "signature_auth": 1.05, "technical_diagram": 0.60,
    },
    "ip": {
        "medical": 0.42, "evidence_citation": 0.80, "financial": 0.90,
        "signature_auth": 0.62, "technical_diagram": 1.05,
    },
    "real_estate": {
        "medical": 0.38, "evidence_citation": 0.85, "financial": 0.92,
        "signature_auth": 0.78, "technical_diagram": 0.95,
    },
    "criminal": {
        "medical": 0.88, "evidence_citation": 1.00, "financial": 0.58,
        "signature_auth": 0.62, "technical_diagram": 0.48,
    },
    "insurance": {
        "medical": 0.92, "evidence_citation": 0.95, "financial": 0.85,
        "signature_auth": 0.58, "technical_diagram": 0.52,
    },
    "evidence_hearing": {
        "medical": 0.90, "evidence_citation": 1.00, "financial": 0.80,
        "signature_auth": 0.72, "technical_diagram": 0.68,
    },
}
_DEFAULT_PHRASE_MULTIPLIERS: Dict[str, float] = {
    "medical": 1.0, "evidence_citation": 1.0, "financial": 1.0,
    "signature_auth": 1.0, "technical_diagram": 1.0,
}


@dataclass
class PertinenceResult:
    """Full output of the pertinence engine."""
    score: float
    trace: List[str] = field(default_factory=list)
    override_valid: bool = False
    override_reason: str = ""
    dominant_factor: str = "none"


class ColorPertinenceEngine:
    """
    Answers: "Does this color matter to the case, or is it just decorative?"

    Computes a pertinence_score from:
      1. Visual evidence strength
      2. Page role legal weight
      3. Cross-page evidence linking (incoming refs, exhibit resolution)
      4. Cluster context
      5. Legal phrase pattern matching (ontology)
      6. TF-IDF relevance

    The pertinence_score IS the final decision driver.
    Overrides are validated against context before being applied.
    """

    def compute(
        self,
        visual: VisualFeatures,
        semantic: SemanticFeatures,
        page_role: str,
        cluster_type: str,
        cluster_importance: float,
        tfidf_score: float,
        tfidf_top_terms: List[str],
        incoming_ref_count: int,
        is_exhibit_resolution: bool,
        text: str,
        override_triggered: bool,
        override_reason: str,
        reference_strength: float = 0.0,       # intent-weighted [0,1] from EvidenceLinkGraph
        bert_score: float = 0.0,               # Legal-BERT score (0 = not run)
        case_type: str = "general_litigation",  # v16: case-aware scoring
        rolling_context_score: float = 0.0,    # DCE: prior-page narrative continuity [0,1]
        prior_directive_count: int = 0,         # DCE: forward directives on the preceding page
        graph_importance: float = 0.0,          # reasoning graph propagated importance [0,1]
        visual_propagation_score: float = 0.0, # VIG: cross-page visual instruction score [0,1]
    ) -> PertinenceResult:
        """
        Compute color pertinence via 5-step reasoning chain.

        Spec formula (weights sum to 1.0):
          pertinence = 0.40 * visual
                     + 0.25 * reference_strength
                     + 0.20 * semantic_combined   (page_role × phrase)
                     + 0.10 * cluster_importance
                     + 0.05 * tfidf_score

        Returns PertinenceResult with score, trace, and validated override.
        """
        trace: List[str] = []
        dominant_factor = "none"
        step = 1

        # ── Step 1: Visual evidence (weight 0.40) ──────────────────
        if visual.photo_regions > 0:
            v_score = min(1.0, 0.70 + visual.photo_regions * 0.10)
            trace.append(
                f"Step {step}: Detected {visual.photo_regions} photo region(s) — "
                f"visual evidence present (score {v_score:.2f})"
            )
            dominant_factor = "photo_evidence"
        elif visual.chart_regions > 0:
            v_score = min(1.0, 0.60 + visual.chart_regions * 0.08)
            trace.append(
                f"Step {step}: Detected {visual.chart_regions} chart/graph region(s) — "
                f"data visualization present (score {v_score:.2f})"
            )
            dominant_factor = "chart_evidence"
        elif visual.stamp_density > 0.001:
            v_score = 0.65
            trace.append(
                f"Step {step}: Official stamp/seal detected "
                f"(density {visual.stamp_density:.4f}) — authenticity marking present"
            )
            dominant_factor = "stamp"
        elif visual.highlight_density > 0.005:
            v_score = 0.55
            trace.append(
                f"Step {step}: Legal text highlighting detected "
                f"(density {visual.highlight_density:.4f}) — annotated content"
            )
            dominant_factor = "highlight"
        elif getattr(visual, 'colored_annotation_density', 0.0) > 0.05:
            ann = visual.colored_annotation_density
            v_score = min(0.70, 0.40 + ann * 0.50)
            trace.append(
                f"Step {step}: Colored attorney annotation text detected "
                f"(density {ann:.3f}) — red/orange markings distributed across body"
            )
            dominant_factor = "colored_annotation"
        elif visual.is_color_meaningful:
            v_score = 0.40
            trace.append(
                f"Step {step}: Color density is meaningful "
                f"({visual.color_density:.3f}) — possible visual evidence"
            )
        else:
            v_score = 0.0
            trace.append(
                f"Step {step}: No significant visual evidence detected "
                f"(density {visual.color_density:.3f})"
            )
        # ── VIG boost: cross-page visual instruction propagation ───────────────
        # When another page in the document contained a visual instruction
        # ("see red text", "refer to highlighted section") and THIS page has
        # the matching visual feature, the VIG propagates a score here.
        # A paralegal reading that instruction would know this page is relevant.
        # We floor v_score to 0.40 when VIG score is strong (≥ 0.20),
        # capped so it never overrides a genuinely stronger visual signal.
        if visual_propagation_score >= 0.20 and v_score < 0.40:
            v_score = max(0.40, min(0.55, v_score + visual_propagation_score * 0.30))
            trace.append(
                f"VIG cross-page boost: visual_propagation_score={visual_propagation_score:.2f} "
                f"→ v_score raised to {v_score:.2f} "
                "(another page's visual instruction applies to this page)"
            )
            if dominant_factor == "none":
                dominant_factor = "cross_page_visual"

        # ── Cross-modal boost: colored annotations + semantic color references ──
        # A human paralegal reasons: "the document mentions 'red' or 'highlighted'
        # AND the page actually has red annotation markings → this is intentional
        # legal markup, not a decoration."
        # Boost v_score when both signals agree.
        _COLOR_TERMS = frozenset({
            'red', 'orange', 'highlighted', 'marked', 'circled',
            'colored', 'annotated', 'blue ink', 'red ink'
        })
        if getattr(visual, 'colored_annotation_density', 0.0) > 0.03:
            semantic_color_ref = any(t in _COLOR_TERMS for t in tfidf_top_terms)
            if not semantic_color_ref and text:
                text_lower = text.lower()
                semantic_color_ref = any(t in text_lower for t in _COLOR_TERMS)
            if semantic_color_ref and v_score < 0.70:
                v_score = min(0.75, v_score + 0.15)
                trace.append(
                    "Cross-modal boost: colored annotation text + semantic color reference "
                    f"(v_score → {v_score:.2f})"
                )
        step += 1

        # ── Step 2: Reference strength (weight 0.25) ───────────────
        # Uses intent-classified strength when available; falls back to count-based
        if reference_strength > 0.0:
            ref_score = reference_strength   # exhibit=1.0, page_ref=0.7 weighted avg
            trace.append(
                f"Step {step}: Reference intent strength = {reference_strength:.2f} "
                f"({incoming_ref_count} ref(s)) — cross-page evidence linking"
            )
            if dominant_factor == "none":
                dominant_factor = "cross_reference"
        elif is_exhibit_resolution:
            ref_score = 0.85
            trace.append(
                f"Step {step}: This page resolves a referenced exhibit — "
                "exhibit body confirmed"
            )
            if dominant_factor == "none":
                dominant_factor = "exhibit_resolution"
        elif incoming_ref_count > 0:
            ref_score = min(1.0, incoming_ref_count * 0.30)
            trace.append(
                f"Step {step}: Referenced by {incoming_ref_count} page(s) — "
                f"evidence linkage detected (score {ref_score:.2f})"
            )
            if dominant_factor == "none":
                dominant_factor = "cross_reference"
        else:
            ref_score = 0.0
            trace.append(f"Step {step}: No incoming cross-page references detected")

        # ── DCE: rolling context, forward directives, visual-attr linking ──
        # Three sub-signals are blended additively into ref_score.  Each is
        # deliberately modest so they amplify rather than override explicit
        # exhibit/page-reference signals.
        dce_boost = 0.0
        text_lower_dce = text.lower() if text else ""

        # Sub-signal A: prior page had forward directive(s) ("as shown below …")
        if prior_directive_count > 0:
            directive_boost = min(0.25, prior_directive_count * 0.10)
            dce_boost += directive_boost
            trace.append(
                f"  DCE-A: {prior_directive_count} forward directive(s) on prior page "
                f"→ +{directive_boost:.2f}"
            )

        # Sub-signal B: rolling narrative continuity from prior pages
        if rolling_context_score > 0.0:
            rolling_boost = rolling_context_score * 0.15
            dce_boost = max(dce_boost, dce_boost + rolling_boost)
            trace.append(
                f"  DCE-B: rolling narrative score = {rolling_context_score:.2f} "
                f"→ +{rolling_boost:.2f}"
            )

        # Sub-signal C: visual attribute cross-reference
        # "see this below in red" + confirmed red visual on THIS page.
        # Detects the instruction on this page (or prior) and confirms the
        # claimed visual attribute exists in the actual image data.
        if detect_legal_instruction_universal(text_lower_dce):
            attr_confirmed, attr_label = _visual_attr_confirmed(text_lower_dce, visual)
            if attr_confirmed:
                attr_boost = 0.20
                dce_boost += attr_boost
                trace.append(
                    f"  DCE-C: instruction + visual attribute '{attr_label}' confirmed "
                    f"in image data → +{attr_boost:.2f}"
                )
                if dominant_factor == "none":
                    dominant_factor = "visual_attr_confirmed"
            else:
                # Instruction present but visual not confirmed — weak signal only
                trace.append(
                    "  DCE-C: legal instruction detected but visual attribute "
                    "not confirmed by image data — no boost"
                )

        if dce_boost > 0.0:
            ref_score = min(1.0, ref_score + dce_boost)
            if dominant_factor == "none" and dce_boost >= 0.10:
                dominant_factor = "cross_page_context"

        step += 1

        # ── Step 3: Semantic + page role combined (weight 0.20) ────
        # v16: case-aware — role weights and phrase scores adapt to case type.
        # When Legal-BERT score is available, it drives semantic_combined (70/30 fusion).
        # Without BERT, falls back to case-adjusted page_role_weight + phrase average.
        role_weight = self._get_case_role_weight(page_role, case_type)
        phrase_score, phrase_label = self._match_case_aware_phrases(text, case_type)
        base_semantic = 0.5 * role_weight + 0.5 * phrase_score

        if bert_score > 0.0:
            # Legal-BERT fusion: 0.70 × bert + 0.30 × base_semantic
            semantic_combined = round(0.70 * bert_score + 0.30 * base_semantic, 4)
            trace.append(
                f"Step {step}: Legal-BERT score={bert_score:.3f} fused with "
                f"base_semantic={base_semantic:.2f} → "
                f"semantic_combined = {semantic_combined:.2f} (BERT active)"
            )
        else:
            semantic_combined = base_semantic
            if phrase_score > 0:
                trace.append(
                    f"Step {step}: Page role '{page_role}' (weight {role_weight:.2f}) + "
                    f"legal phrase '{phrase_label}' (score {phrase_score:.2f}) → "
                    f"semantic_combined = {semantic_combined:.2f}"
                )
            else:
                trace.append(
                    f"Step {step}: Page role '{page_role}' (weight {role_weight:.2f}), "
                    f"no legal phrase match → semantic_combined = {semantic_combined:.2f}"
                )
        if dominant_factor == "none" and role_weight >= 0.80:
            dominant_factor = f"page_role:{page_role}"
        step += 1

        # ── Semantic gate v2: hard-cutoff tiers ──────────────────────────
        # High-authority direct evidence (photos, charts, stamps, exhibit resolutions,
        # and page-role classified pages) are self-evidently probative and bypass gating.
        # For everything else, thin semantic content is treated as absence of context:
        #
        #   semantic < 0.20 → scale = 0.00  (zero out visual + ref — dead signal)
        #   semantic 0.20–0.35 → scale = 0.25  (heavily suppressed — weak context)
        #   semantic ≥ 0.35 → scale = 1.00  (full pass-through — contextually justified)
        #
        # Additionally, cross-page citations (refs) require semantic ≥ 0.25 to carry
        # any weight at all.  A cited page with near-zero semantic has no standing.
        _GATE_EXEMPT = frozenset({
            "photo_evidence", "chart_evidence", "stamp", "exhibit_resolution",
        })
        if (dominant_factor not in _GATE_EXEMPT
                and not dominant_factor.startswith("page_role:")):
            # Tier gate on visual and reference
            if semantic_combined < 0.20:
                _sem_scale = 0.0
            elif semantic_combined < 0.35:
                _sem_scale = 0.25
            else:
                _sem_scale = 1.0
            if _sem_scale < 1.0:
                v_score   = round(v_score   * _sem_scale, 4)
                ref_score = round(ref_score * _sem_scale, 4)
                trace.append(
                    f"Semantic gate [{dominant_factor}]: "
                    f"semantic={semantic_combined:.2f} → scale={_sem_scale:.2f} — "
                    f"v_score→{v_score:.2f}, ref_score→{ref_score:.2f}"
                )
            # Hard ref kill: citations have no legal standing without semantic context
            if semantic_combined < 0.25 and ref_score > 0.0:
                ref_score = 0.0
                trace.append(
                    f"Ref kill: semantic={semantic_combined:.2f} < 0.25 — "
                    "cross-page citations voided (no standing without semantic context)"
                )

        # ── Early exit: all signals negligible after gating ───────────────
        # If visual, reference, and semantic are all effectively zero after the gate
        # has run, the formula will produce a negligible score regardless of cluster
        # or TF-IDF.  Return B/W immediately to avoid polluting reasoning traces.
        if v_score < 0.05 and ref_score < 0.05 and semantic_combined < 0.15:
            trace.append(
                "Early exit: v_score, ref_score, and semantic_combined all negligible "
                "after semantic gate — page classified B/W (score=0.10)"
            )
            return PertinenceResult(
                score=0.10,
                trace=trace,
                override_valid=False,
                override_reason="",
                dominant_factor=dominant_factor if dominant_factor != "none" else "none",
            )

        # ── Step 4: Cluster context + graph importance (weight 0.10) ───
        # Blend cluster_importance with the reasoning graph's propagated score.
        # The graph reflects chain reasoning (a page cited by a strong page
        # inherits importance); the cluster reflects document-level grouping.
        # Weighted blend: 0.60 cluster + 0.40 graph (graph is more specific).
        if graph_importance > 0.0:
            effective_cluster = round(
                0.60 * cluster_importance + 0.40 * graph_importance, 4
            )
            trace.append(
                f"Step {step}: Cluster '{cluster_type}' (importance {cluster_importance:.2f}) "
                f"blended with graph importance {graph_importance:.2f} "
                f"→ effective {effective_cluster:.2f}"
            )
        else:
            effective_cluster = cluster_importance
            trace.append(
                f"Step {step}: Cluster type '{cluster_type}' — "
                f"cluster importance {cluster_importance:.2f}"
            )
        cluster_importance = effective_cluster
        step += 1

        # ── Step 5: TF-IDF legal relevance (weight 0.05) ───────────
        tfidf_clamped = max(0.0, min(1.0, tfidf_score))
        terms_str = ", ".join(tfidf_top_terms[:3]) if tfidf_top_terms else "none"
        trace.append(
            f"Step {step}: TF-IDF legal relevance = {tfidf_clamped:.3f} "
            f"(top terms: {terms_str})"
        )
        step += 1

        # ── Pertinence score (spec formula) ────────────────────────
        raw_score = (
            0.40 * v_score +
            0.25 * ref_score +
            0.20 * semantic_combined +
            0.10 * cluster_importance +
            0.05 * tfidf_clamped
        )
        pertinence_score = _soft_scale(raw_score)

        # ── Contextual override validation ──────────────────────────
        validated_override = False
        validated_override_reason = ""
        if override_triggered:
            validated_override, validated_override_reason = self._validate_override(
                visual, semantic, page_role, cluster_type,
                incoming_ref_count, is_exhibit_resolution,
                phrase_score, pertinence_score, override_reason,
            )
            if validated_override:
                pertinence_score = min(0.95, pertinence_score + 0.30)
                trace.append(
                    f"Step {step}: Override VALIDATED — '{validated_override_reason}' — "
                    f"pertinence boosted → {pertinence_score:.3f}"
                )
            else:
                trace.append(
                    f"Step {step}: Override present but CONTEXT DOES NOT SUPPORT it "
                    f"('{override_reason}') — score unchanged at {pertinence_score:.3f}"
                )
            step += 1

        # ── v13: Pertinence safety cap — penalize visually weak pages ──────────
        ve_score = getattr(visual, "visual_evidence_score", 1.0)
        if ve_score < 0.2:
            pre_cap = pertinence_score
            pertinence_score = round(pertinence_score * 0.6, 4)
            trace.append(
                f"Safety cap: visual_evidence_score={ve_score:.3f} < 0.2 — "
                f"pertinence reduced {pre_cap:.4f} → {pertinence_score:.4f}"
            )

        pertinence_score = round(pertinence_score, 4)

        # 3-zone verdict aligned with decision thresholds
        if pertinence_score >= 0.75:
            verdict = "color MATTERS to this case (HIGH — COLOR)"
        elif pertinence_score <= 0.25:
            verdict = "color is DECORATIVE (LOW — B/W)"
        else:
            verdict = "color relevance UNCERTAIN (MEDIUM — REVIEW REQUIRED)"

        trace.append(
            f"Conclusion: Pertinence score = {pertinence_score:.3f} — {verdict}"
        )

        return PertinenceResult(
            score=pertinence_score,
            trace=trace,
            override_valid=validated_override,
            override_reason=validated_override_reason,
            dominant_factor=dominant_factor,
        )

    # ──────────────────────────────────────────────────────────
    # Case-Aware Role Weight
    # ──────────────────────────────────────────────────────────
    def _get_case_role_weight(self, page_role: str, case_type: str) -> float:
        """
        v16: Return case-adjusted legal relevance weight for a page role.
        Falls back to default PAGE_ROLE_WEIGHTS for unknown case types.
        """
        case_weights = _CASE_ROLE_WEIGHTS.get(case_type)
        if case_weights:
            return case_weights.get(page_role, PAGE_ROLE_WEIGHTS.get(page_role, 0.30))
        return PAGE_ROLE_WEIGHTS.get(page_role, 0.30)

    # ──────────────────────────────────────────────────────────
    # Case-Aware Phrase Pattern Matching (Legal Knowledge Ontology)
    # ──────────────────────────────────────────────────────────
    def _match_case_aware_phrases(self, text: str, case_type: str = "general_litigation") -> Tuple[float, str]:
        """
        v16: Case-aware phrase matching.

        The base score for each phrase category is multiplied by how relevant
        that category is to the current case type:
          - PI case:       medical phrases boosted, financial discounted
          - Contract case: signature/auth phrases boosted, medical discounted
          - IP case:       technical diagram phrases boosted
          - Criminal:      evidence citation + medical at full weight
          - Default:       all categories at 1.0 multiplier

        Returns (score, matched_phrase).
        """
        if not text:
            return 0.0, ""
        text_lower = text.lower()
        mults = _CASE_PHRASE_MULTIPLIERS.get(case_type, _DEFAULT_PHRASE_MULTIPLIERS)

        for phrase in MEDICAL_EVIDENCE_PHRASES:
            if phrase in text_lower:
                return min(1.0, round(0.90 * mults.get("medical", 1.0), 4)), phrase

        for phrase in SIGNATURE_AUTH_PHRASES:
            if phrase in text_lower:
                return min(1.0, round(0.85 * mults.get("signature_auth", 1.0), 4)), phrase

        for phrase in TECHNICAL_DIAGRAM_PHRASES:
            if phrase in text_lower:
                return min(1.0, round(0.85 * mults.get("technical_diagram", 1.0), 4)), phrase

        for phrase in EVIDENCE_CITATION_PHRASES:
            if phrase in text_lower:
                return min(1.0, round(0.75 * mults.get("evidence_citation", 1.0), 4)), phrase

        for phrase in FINANCIAL_EVIDENCE_PHRASES:
            if phrase in text_lower:
                return min(1.0, round(0.70 * mults.get("financial", 1.0), 4)), phrase

        # ── Universal instruction fallback ─────────────────────────
        # None of the exact phrase lists matched, but the text may still contain
        # a loosely-phrased legal directive ("see this below in red").
        # Use the intent-structure detector as a last-resort signal at a lower
        # base score (0.35) so it doesn't compete with confirmed exact matches.
        if detect_legal_instruction_universal(text_lower):
            base = 0.35 * mults.get("evidence_citation", 1.0)
            return round(min(0.50, base), 4), "universal_instruction_detected"

        return 0.0, ""

    def _match_legal_phrases(self, text: str) -> Tuple[float, str]:
        """Backward-compatible alias — uses default (general litigation) multipliers."""
        return self._match_case_aware_phrases(text, "general_litigation")

    # ──────────────────────────────────────────────────────────
    # Contextual Override Validation
    # ──────────────────────────────────────────────────────────
    def _validate_override(
        self,
        visual: VisualFeatures,
        semantic: SemanticFeatures,
        page_role: str,
        cluster_type: str,
        incoming_ref_count: int,
        is_exhibit_resolution: bool,
        phrase_score: float,
        pertinence_score: float,
        original_reason: str,
    ) -> Tuple[bool, str]:
        """
        Validates whether a raw override trigger is actually contextually justified.

        Rule: A trigger fires only when at least ONE of these is true:
          - Page role is legally significant
          - Incoming references exist (someone cites this page)
          - Exhibit resolution detected
          - Legal phrase pattern matched in text
          - Cluster is evidence or mixed type
          - Pertinence score already above 0.3 (sufficient intrinsic evidence)
        """
        # v13: visual_evidence_score hard gate for photo/chart overrides
        ve_score = getattr(visual, "visual_evidence_score", 1.0)

        # Photo trigger: valid if role suggests it matters OR cross-referenced
        if "evidence photos" in original_reason or "photo" in original_reason:
            # Role-confirmed check FIRST: if the page classifier assigned evidence_photo or
            # medical_image, that role is itself the contextual confirmation — the visual
            # analyzer detected photo regions AND the classifier accepted them.  The ve_score
            # gate (which checks area-ratio) is irrelevant here: a small photo has a small
            # area ratio but is still a real photo that must print in color.
            if page_role in ("evidence_photo", "medical_image", "exhibit_page"):
                return True, f"Photo evidence confirmed by role '{page_role}'"
            # For ambiguous roles: fall back to ve_score gate
            if ve_score < 0.4:
                return False, ""
            if incoming_ref_count > 0:
                return True, f"Photo page is cross-referenced by {incoming_ref_count} page(s)"
            if is_exhibit_resolution:
                return True, "Photo page resolves a referenced exhibit"
            if pertinence_score >= 0.30:
                return True, f"Photo evidence with sufficient context (pertinence {pertinence_score:.2f})"
            return False, ""

        # Highlight trigger: valid if legal text is present
        if "highlighted" in original_reason:
            if phrase_score > 0 or semantic.exhibit_mentions > 0:
                return True, "Highlighted text with legal citation context"
            if pertinence_score >= 0.30:
                return True, "Highlighted legal document content"
            return False, ""

        # Stamp/seal: valid if role or cluster supports it
        if "stamp" in original_reason or "seal" in original_reason:
            if page_role in ("signature_page", "exhibit_page", "evidence_photo"):
                return True, f"Official stamp on '{page_role}' page"
            if cluster_type in ("evidence_cluster", "mixed_cluster"):
                return True, "Stamp in evidence document cluster"
            return False, ""

        # Exhibit mention trigger: valid if evidence-type cluster or incoming refs
        if "exhibit" in original_reason:
            if cluster_type in ("evidence_cluster", "mixed_cluster"):
                return True, "Exhibit reference in evidence cluster"
            if pertinence_score >= 0.25:
                return True, f"Exhibit reference with legal context (pertinence {pertinence_score:.2f})"
            return False, ""

        # Chart trigger: valid if financial or technical context
        if "chart" in original_reason:
            # v13: require visual confirmation for chart overrides
            if ve_score < 0.4:
                return False, ""
            if page_role == "financial_chart":
                return True, "Financial chart — data visualization is legally relevant"
            if cluster_type == "evidence_cluster":
                return True, "Chart in evidence cluster"
            return False, ""

        # Fallback: trust the override if pertinence is non-trivial
        if pertinence_score >= 0.35:
            return True, f"Override contextually supported (pertinence {pertinence_score:.2f})"

        return False, ""
