"""
CPCE - Visual Instruction Propagation Graph

A human paralegal reads:
  Page 2: "see the red text below"
  Page 5: "refer to the highlighted sections"

and knows those instructions apply to pages whose visual features confirm the
referenced content — even when no explicit page number is cited.

This module makes that inference structural:

  1. Scan each page for visual instructions (instruction verb + color/visual keyword).
  2. Extract which visual feature is referenced ("red" → colored_annotation_density,
     "highlighted" → highlight_density, "chart" → chart_regions, …).
  3. Find all pages in the document whose measured visual features confirm the match.
  4. Score each match by instruction strength × distance decay (0.90 per page, cutoff 15).
  5. Expose a visual_propagation_score per page and human-readable context strings.

Intent strength assignments:
  color keyword ("red", "orange")    0.55  — specific visual attribute named
  visual keyword + reference word    0.50  — explicit visual + directional instruction
  visual keyword only                0.40  — visual keyword present, no direction
  generic forward directive          0.35  — direction word present, no color named
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────
# Vocabulary (mirrors EvidenceLinkGraph, kept local for independence)
# ──────────────────────────────────────────────────────────────

_INSTRUCTION_VERBS = frozenset([
    "see", "refer", "check", "review", "examine", "inspect",
    "consider", "note", "observe", "consult", "view",
])
_REFERENCE_WORDS = frozenset([
    "below", "above", "following", "attached", "herein", "hereto",
    "next", "subsequent", "previous", "appended", "enclosed",
])

# Direction vocabulary: determines whether an instruction points forward, backward, or both.
# "see red below" → forward only.  "see red above" → backward only.
_FORWARD_WORDS  = frozenset(["below", "following", "next", "subsequent", "attached",
                              "appended", "enclosed", "hereto"])
_BACKWARD_WORDS = frozenset(["above", "previous", "prior", "preceding"])

# Mapping: color/visual keyword → (VisualFeatures attribute, threshold, intent_strength)
# threshold = 0 means integer field (photo_regions, chart_regions) just needs to be > 0
_FEATURE_MAP: Dict[str, Tuple[str, float, float]] = {
    "red":           ("colored_annotation_density", 0.03, 0.55),
    "orange":        ("colored_annotation_density", 0.03, 0.55),
    "highlighted":   ("highlight_density",          0.001, 0.50),
    "highlight":     ("highlight_density",          0.001, 0.50),
    "yellow":        ("highlight_density",          0.001, 0.45),
    "green":         ("highlight_density",          0.001, 0.45),
    "stamp":         ("stamp_density",              0.002, 0.50),
    "seal":          ("stamp_density",              0.002, 0.50),
    "photo":         ("photo_regions",              0.0,   0.50),
    "photograph":    ("photo_regions",              0.0,   0.50),
    "picture":       ("photo_regions",              0.0,   0.50),
    "image":         ("photo_regions",              0.0,   0.50),
    "figure":        ("photo_regions",              0.0,   0.45),
    "chart":         ("chart_regions",              0.0,   0.50),
    "graph":         ("chart_regions",              0.0,   0.50),
    "diagram":       ("chart_regions",              0.0,   0.45),
    "illustration":  ("chart_regions",              0.0,   0.45),
}


# ──────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────

@dataclass
class VisualInstruction:
    """A visual instruction detected on a source page."""
    source_page:      int
    sentence:         str    # the raw matched sentence
    color_keyword:    str    # e.g. "red"
    feature_attr:     str    # VisualFeatures attribute name
    threshold:        float  # matching threshold for the attribute
    intent_strength:  float  # 0.35 – 0.55
    direction:        str    = "forward"  # "forward" | "backward" | "any"
    # "forward" = target pages must come AFTER source (e.g. "see red below")
    # "backward" = target pages must come BEFORE source (e.g. "see red above")
    # "any" = no directional word found — default to forward-only for safety


@dataclass
class VisualPropagationEdge:
    """A directed link from an instruction source page to a matching target page."""
    source_page:  int
    target_page:  int
    instruction:  VisualInstruction
    distance:     int    # absolute page distance
    score:        float  # intent_strength × (DECAY ** distance)
    context_str:  str    # human-readable explanation


# ──────────────────────────────────────────────────────────────
# Main class
# ──────────────────────────────────────────────────────────────

class VisualInstructionGraph:
    """
    Cross-page visual meaning propagation graph for CPCE.

    Build order: after Stage 4b (LegalReasoningGraph), before Stage 7 scoring.
    Reads:  page texts, List[VisualFeatures]
    Exposes: get_visual_propagation_score(), get_visual_instruction_context()
    """

    DISTANCE_DECAY:  float = 0.90   # score multiplier per page of distance
    DISTANCE_CUTOFF: int   = 15     # maximum pages to propagate across
    MIN_SCORE:       float = 0.08   # edges below this threshold are discarded

    def __init__(self) -> None:
        self._instructions: List[VisualInstruction] = []
        self._edges:        List[VisualPropagationEdge] = []
        # page_id → best propagation score reaching that page
        self._scores:       Dict[int, float] = {}
        # page_id → list of context strings (sorted by score)
        self._contexts:     Dict[int, List[str]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def build(
        self,
        texts: List[str],
        visual_features: List,   # List[VisualFeatures]
    ) -> None:
        """
        Scan all pages for visual instructions, match them to pages with
        confirming visual features, score and store propagation edges.
        """
        self._instructions = []
        self._edges        = []
        self._scores       = {}
        self._contexts     = {}

        num_pages = len(texts)

        # ── Pass 1: extract all visual instructions ───────────────────────────
        for i, text in enumerate(texts):
            if not text:
                continue
            self._instructions.extend(self._extract_instructions(i, text))

        # ── Pass 2: for each instruction, find matching target pages ──────────
        for instr in self._instructions:
            edges = self._find_targets(instr, visual_features, num_pages)
            self._edges.extend(edges)

        # ── Pass 3: aggregate scores and context strings per target page ──────
        edge_map: Dict[int, List[VisualPropagationEdge]] = {}
        for e in self._edges:
            edge_map.setdefault(e.target_page, []).append(e)

        for pid, edges in edge_map.items():
            edges.sort(key=lambda e: e.score, reverse=True)
            self._scores[pid] = round(min(1.0, edges[0].score), 4)
            self._contexts[pid] = [e.context_str for e in edges[:5]]

    def get_visual_propagation_score(self, page_id: int) -> float:
        """
        Best single-edge propagation score reaching this page [0, 1].
        Returns 0.0 when no instruction targets this page.
        """
        return self._scores.get(page_id, 0.0)

    def get_visual_instruction_context(self, page_id: int) -> List[str]:
        """
        Human-readable explanation strings for every instruction edge
        targeting this page, sorted by score descending (max 5).
        """
        return list(self._contexts.get(page_id, []))

    def summary(self) -> Dict:
        """Debug summary."""
        from collections import defaultdict
        kw_counts: Dict[str, int] = defaultdict(int)
        for instr in self._instructions:
            kw_counts[instr.color_keyword] += 1
        return {
            "total_instructions": len(self._instructions),
            "total_edges":        len(self._edges),
            "pages_affected":     len(self._scores),
            "keyword_counts":     dict(kw_counts),
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _extract_instructions(
        self, page_idx: int, text: str
    ) -> List[VisualInstruction]:
        """
        Sentence-level scan for visual instructions.

        A sentence qualifies when:
          • it contains an instruction verb  (see / refer / note …)  AND
          • it contains a keyword present in _FEATURE_MAP

        Intent strength is determined by co-presence of:
          • color keyword (red/orange)              → 0.55
          • non-color visual + reference word       → 0.50
          • non-color visual keyword only           → 0.40
          • instruction verb + reference word only  → 0.35 (no visual keyword in map)
        """
        instructions: List[VisualInstruction] = []
        sentences = re.split(r'[.\n;]', text.lower())

        for sent in sentences:
            words = set(re.findall(r'\b\w+\b', sent))

            if not (words & _INSTRUCTION_VERBS):
                continue  # no instruction verb present

            # Find first matching feature keyword
            matched_kw = None
            for kw in _FEATURE_MAP:
                if kw in words:
                    matched_kw = kw
                    break

            if matched_kw is None:
                continue  # no visual/color keyword present

            feat_attr, threshold, base_strength = _FEATURE_MAP[matched_kw]

            # Determine direction: forward / backward / any
            has_forward  = bool(words & _FORWARD_WORDS)
            has_backward = bool(words & _BACKWARD_WORDS)
            if has_forward and not has_backward:
                direction = "forward"
            elif has_backward and not has_forward:
                direction = "backward"
            else:
                # No clear direction — default forward-only so we never
                # wrongly promote pages that come BEFORE the instruction.
                direction = "forward"

            # Boost strength when both a color keyword AND a reference word are present
            has_ref = has_forward or has_backward
            if matched_kw in ("red", "orange"):
                intent_strength = 0.55 + (0.05 if has_ref else 0.0)
            elif has_ref:
                intent_strength = base_strength + 0.05
            else:
                intent_strength = base_strength

            intent_strength = round(min(0.75, intent_strength), 3)

            instructions.append(VisualInstruction(
                source_page=page_idx,
                sentence=sent.strip()[:120],
                color_keyword=matched_kw,
                feature_attr=feat_attr,
                threshold=threshold,
                intent_strength=intent_strength,
                direction=direction,
            ))

        return instructions

    def _find_targets(
        self,
        instr: VisualInstruction,
        visual_features: List,
        num_pages: int,
    ) -> List[VisualPropagationEdge]:
        """
        Find all pages whose visual features confirm the instructed attribute,
        within DISTANCE_CUTOFF pages of the source. Both forward and backward.
        """
        edges: List[VisualPropagationEdge] = []

        feat_attr  = instr.feature_attr
        threshold  = instr.threshold
        is_int_field = threshold == 0.0  # photo_regions, chart_regions are ints

        for target_idx in range(num_pages):
            if target_idx == instr.source_page:
                continue

            # Enforce direction: forward = target must be AFTER source;
            # backward = target must be BEFORE source.
            if instr.direction == "forward" and target_idx < instr.source_page:
                continue
            if instr.direction == "backward" and target_idx > instr.source_page:
                continue

            distance = abs(target_idx - instr.source_page)
            if distance > self.DISTANCE_CUTOFF:
                continue

            # Check if target page has the matching visual feature
            vf = visual_features[target_idx] if target_idx < len(visual_features) else None
            if vf is None:
                continue

            feat_val = getattr(vf, feat_attr, 0)
            if is_int_field:
                qualifies = int(feat_val) > 0
            else:
                qualifies = float(feat_val) > threshold

            if not qualifies:
                continue

            score = round(
                instr.intent_strength * (self.DISTANCE_DECAY ** distance),
                4,
            )
            if score < self.MIN_SCORE:
                continue

            direction = "after" if target_idx > instr.source_page else "before"
            context_str = (
                f"Page {instr.source_page + 1} instructed '{instr.sentence[:80]}' "
                f"→ page {target_idx + 1} ({distance} pages {direction}) "
                f"contains {_attr_label(feat_attr, feat_val)} that matches"
            )

            edges.append(VisualPropagationEdge(
                source_page=instr.source_page,
                target_page=target_idx,
                instruction=instr,
                distance=distance,
                score=score,
                context_str=context_str,
            ))

        return edges


# ── Helper ────────────────────────────────────────────────────────────────────

def _attr_label(feat_attr: str, feat_val) -> str:
    """Human-readable label for a visual feature attribute value."""
    if feat_attr == "colored_annotation_density":
        return f"red/orange annotation text (density {float(feat_val):.3f})"
    if feat_attr == "highlight_density":
        return f"highlighted text (density {float(feat_val):.4f})"
    if feat_attr == "stamp_density":
        return f"a stamp or seal (density {float(feat_val):.4f})"
    if feat_attr == "photo_regions":
        return f"{int(feat_val)} photo region(s)"
    if feat_attr == "chart_regions":
        return f"{int(feat_val)} chart/graph region(s)"
    return f"{feat_attr}={feat_val}"
