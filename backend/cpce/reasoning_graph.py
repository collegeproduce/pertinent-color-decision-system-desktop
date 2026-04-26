"""
CPCE - Cross-Page Legal Reasoning Graph

Treats each page as a node and each cross-page relationship as a typed,
weighted directed edge.  After all edges are created, an iterative
PageRank-style propagation computes a graph_importance score per page
that reflects its position in the document's evidentiary chain — not
just its individual signals.

Edge types and intent weights
──────────────────────────────
  exhibit_ref    1.00   "See Exhibit A" → the exhibit page
  page_ref       0.70   "See page 5" → target page
  directive      0.35   "as shown below" → next page
  visual_match   0.30   color mention in text → page whose visual confirms it
  semantic_sim   0.50   BERT cosine similarity ≥ threshold (both directions)
  sequential     0.10   adjacent pages (weak background context link)

Propagation
───────────
  importance[i] = base[i]
                + DAMPING × Σ ( edge_weight × importance[source] )

Run for ITERATIONS rounds.  A page cited by a strongly-important page
inherits more weight than one cited by a weak page — true chain reasoning.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from collections import defaultdict

import numpy as np


# ─────────────────────────────────────────────────────────────
# Edge weight table
# ─────────────────────────────────────────────────────────────
EDGE_WEIGHTS: Dict[str, float] = {
    "exhibit_ref":   1.00,
    "page_ref":      0.70,
    "directive":     0.35,
    "visual_match":  0.30,
    "semantic_sim":  0.50,
    "sequential":    0.10,
}


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class ReasoningEdge:
    """A directed edge source → target with an evidence description."""
    source: int
    target: int
    edge_type: str           # key in EDGE_WEIGHTS
    weight: float
    evidence: str            # human-readable paralegal description


@dataclass
class PageNode:
    """A page as a graph node."""
    page_id: int
    page_role: str = "unknown"
    base_importance: float = 0.0      # seed score from visual / semantic / role
    graph_importance: float = 0.0     # after full-graph propagation (for explanations)
    decision_importance: float = 0.0  # after strict-graph propagation (for scoring)
    incoming_edges: List[ReasoningEdge] = field(default_factory=list)
    outgoing_edges: List[ReasoningEdge] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Main class
# ─────────────────────────────────────────────────────────────

class LegalReasoningGraph:
    """
    Cross-Page Legal Reasoning Graph for CPCE.

    Usage (in engine pipeline after EvidenceLinkGraph.build):
        graph = LegalReasoningGraph()
        graph.build(num_pages, page_roles, visual_features,
                    semantic_features, evidence_link_graph)
        importance = graph.get_graph_importance(page_idx)
        chain      = graph.get_reasoning_chain(page_idx)
    """

    DAMPING    = 0.65    # fraction of neighbor importance that flows in (reasoning graph)
    ITERATIONS = 3       # propagation rounds (sufficient for <200 pages)
    SIM_THRESHOLD = 0.75 # BERT cosine threshold for semantic_sim edges

    # ── Materiality gating ──────────────────────────────────────────────
    # Only edges in MATERIAL_EDGE_TYPES propagate in the reasoning graph.
    MATERIAL_EDGE_TYPES = frozenset({"exhibit_ref", "visual_match", "directive", "page_ref"})

    # ── Decision-graph legal weighting matrix ────────────────────────────
    # Mimics how a paralegal weights different signal types.
    # Hard evidentiary links carry full weight; procedural and semantic links
    # are dampened but NOT discarded — they still contribute supporting context.
    #
    #   exhibit_ref   1.00  → directly identifies exhibit pages
    #   visual_match  0.90  → confirms a visual signal was actually present
    #   directive     0.40  → legal instruction ("see below", "refer to p5")
    #   page_ref      0.30  → explicit cross-page citation
    #   semantic_sim  0.20  → related legal content (clauses, definitions)
    #   sequential    0.00  → adjacency has no independent legal weight
    #
    # Non-listed types default to 0.0 (ignored).
    DECISION_WEIGHTS: Dict[str, float] = {
        "exhibit_ref":  1.00,
        "visual_match": 0.90,
        "directive":    0.40,
        "page_ref":     0.30,
        "semantic_sim": 0.20,
        "sequential":   0.00,
    }

    # Per-hop decay: each additional hop multiplies the carried weight by this factor.
    # Long weak chains do NOT accumulate to strong influence.
    PROPAGATION_DECAY = 0.85

    # Minimum accumulated path weight to allow propagation at all.
    MIN_PATH_WEIGHT = 0.25

    def __init__(self) -> None:
        self.nodes: Dict[int, PageNode] = {}
        self._edges: List[ReasoningEdge] = []

    # ── Public API ───────────────────────────────────────────────────

    def build(
        self,
        num_pages: int,
        page_roles: List[str],
        visual_features: List,          # List[VisualFeatures]
        semantic_features: List,        # List[SemanticFeatures]
        link_graph,                     # EvidenceLinkGraph
        bert_embeddings: Optional[List[Optional[np.ndarray]]] = None,
    ) -> None:
        """
        Build the reasoning graph from all available CPCE signals.

        Steps:
          1. Create one PageNode per page with a base_importance seed.
          2. Add typed edges from EvidenceLinkGraph (exhibit refs, page refs,
             forward directives, color-match refs).
          3. Add weak sequential edges between adjacent pages.
          4. Optionally add BERT semantic-similarity edges.
          5. Run iterative propagation.
        """
        self.nodes = {}
        self._edges = []

        # ── Step 1: nodes ─────────────────────────────────────────
        for i in range(num_pages):
            vf   = visual_features[i]   if i < len(visual_features)   else None
            sf   = semantic_features[i] if i < len(semantic_features)  else None
            role = page_roles[i]        if i < len(page_roles)         else "unknown"
            base = self._compute_base_importance(vf, sf, role)
            self.nodes[i] = PageNode(page_id=i, page_role=role,
                                     base_importance=base, graph_importance=base)

        # ── Step 2: edges from EvidenceLinkGraph ──────────────────
        exhibit_locations: Dict[str, int] = getattr(link_graph, '_exhibit_location', {})

        for i in range(num_pages):
            link = link_graph.get_link_info(i)

            for ref_str in getattr(link, 'outgoing_refs', []):

                # Exhibit reference: page i → exhibit body page
                if ref_str.startswith("exhibit_"):
                    label  = ref_str[8:].upper()
                    target = exhibit_locations.get(label)
                    if target is not None and target != i:
                        vf_target = (visual_features[target]
                                     if target < len(visual_features) else None)
                        self._add_edge(
                            i, target, "exhibit_ref",
                            f"Page {i+1} references Exhibit {label} "
                            f"→ located on page {target+1}"
                        )

                # Explicit page reference: page i → target page
                elif ref_str.startswith("page_"):
                    try:
                        t1 = int(re.search(r'\d+', ref_str).group())
                        t  = t1 - 1
                        if 0 <= t < num_pages and t != i:
                            self._add_edge(
                                i, t, "page_ref",
                                f"Page {i+1} cites page {t1}"
                            )
                    except (AttributeError, ValueError):
                        pass

                # Forward directive: "as shown below" → next page
                elif ref_str.startswith("forward_directive"):
                    target = i + 1
                    if target < num_pages:
                        self._add_edge(
                            i, target, "directive",
                            f"Page {i+1} instructs 'see below / following' "
                            f"→ page {target+1} is the referenced content"
                        )

                # Color match: text color mention → page with confirmed visual
                elif ref_str.startswith("color_ref_to_page_"):
                    try:
                        t1     = int(re.search(r'\d+$', ref_str).group())
                        target = t1 - 1
                        if 0 <= target < num_pages and target != i:
                            vf_t = (visual_features[target]
                                    if target < len(visual_features) else None)
                            clabel = self._color_evidence_label(vf_t)
                            self._add_edge(
                                i, target, "visual_match",
                                f"Page {i+1} mentions a color marker — "
                                f"page {t1} has {clabel} that matches"
                            )
                    except (AttributeError, ValueError):
                        pass

        # ── Step 3: sequential edges ───────────────────────────────
        for i in range(num_pages - 1):
            self._add_edge(
                i, i + 1, "sequential",
                f"Pages {i+1} and {i+2} are adjacent"
            )

        # ── Step 4: BERT semantic similarity edges ─────────────────
        if bert_embeddings and len(bert_embeddings) == num_pages:
            for i in range(num_pages):
                for j in range(i + 1, num_pages):
                    ei, ej = bert_embeddings[i], bert_embeddings[j]
                    if ei is not None and ej is not None:
                        sim = self._cosine(ei, ej)
                        if sim >= self.SIM_THRESHOLD:
                            desc = (f"Pages {i+1} and {j+1} share similar "
                                    f"legal content (similarity {sim:.2f})")
                            self._add_edge(i, j, "semantic_sim", desc)
                            self._add_edge(j, i, "semantic_sim", desc)

        # ── Step 5: propagate ──────────────────────────────────────
        self._propagate()           # reasoning graph  (full, for explanations)
        self._propagate_decision()  # decision graph   (strict, for scoring)

    def get_graph_importance(self, page_id: int) -> float:
        """Full-graph propagated importance [0, 1] — used for reasoning explanations."""
        node = self.nodes.get(page_id)
        return round(node.graph_importance, 4) if node else 0.0

    def get_decision_importance(self, page_id: int) -> float:
        """Strict-graph propagated importance [0, 1] — used for pertinence scoring."""
        node = self.nodes.get(page_id)
        return round(node.decision_importance, 4) if node else 0.0

    def get_reasoning_chain(self, page_id: int) -> List[str]:
        """
        Return up to 3 human-readable evidence strings from the strongest
        incoming edges.  Used verbatim in the paralegal reasoning output.
        Excludes weak sequential edges.
        """
        node = self.nodes.get(page_id)
        if not node:
            return []
        meaningful = [e for e in node.incoming_edges
                      if e.edge_type != "sequential"]
        meaningful.sort(key=lambda e: e.weight, reverse=True)
        return [e.evidence for e in meaningful[:3]]

    def get_strongest_incoming_edge(self, page_id: int) -> Optional[ReasoningEdge]:
        """Return the single highest-weight incoming edge (skipping sequential)."""
        node = self.nodes.get(page_id)
        if not node:
            return None
        candidates = [e for e in node.incoming_edges if e.edge_type != "sequential"]
        return max(candidates, key=lambda e: e.weight) if candidates else None

    def summary(self) -> Dict:
        """Debug summary of graph structure and average importance."""
        type_counts = defaultdict(int)
        for e in self._edges:
            type_counts[e.edge_type] += 1
        avg_imp = (round(float(np.mean([n.graph_importance
                                        for n in self.nodes.values()])), 4)
                   if self.nodes else 0.0)
        return {
            "total_nodes":          len(self.nodes),
            "total_edges":          len(self._edges),
            "edge_type_counts":     dict(type_counts),
            "avg_graph_importance": avg_imp,
        }

    # ── Internal helpers ─────────────────────────────────────────────

    def _add_edge(self, source: int, target: int,
                  edge_type: str, evidence: str) -> None:
        weight = EDGE_WEIGHTS.get(edge_type, 0.20)
        edge   = ReasoningEdge(source=source, target=target,
                               edge_type=edge_type, weight=weight,
                               evidence=evidence)
        self._edges.append(edge)
        if source in self.nodes:
            self.nodes[source].outgoing_edges.append(edge)
        if target in self.nodes:
            self.nodes[target].incoming_edges.append(edge)

    def _compute_base_importance(self, vf, sf, role: str) -> float:
        """Seed importance from visual evidence, semantic signals, and page role."""
        score = 0.0

        if vf is not None:
            if getattr(vf, 'photo_regions', 0) > 0:
                score += 0.60
            elif getattr(vf, 'grayscale_regions', 0) > 0:
                score += 0.35
            if getattr(vf, 'chart_regions', 0) > 0:
                score += 0.30
            if getattr(vf, 'highlight_density', 0) > 0.001:
                score += 0.20
            if getattr(vf, 'stamp_density', 0) > 0.0005:
                score += 0.25
            if getattr(vf, 'signature_regions', 0) > 0:
                score += 0.20
            if getattr(vf, 'bw_stamp_regions', 0) > 0:
                score += 0.15

        if sf is not None:
            ex = getattr(sf, 'exhibit_mentions', 0)
            if ex > 0:
                score += min(0.30, ex * 0.10)

        role_bonus: Dict[str, float] = {
            "medical_image":   0.40,
            "evidence_photo":  0.35,
            "exhibit_page":    0.30,
            "financial_chart": 0.25,
            "signature_page":  0.15,
        }
        score += role_bonus.get(role, 0.0)

        return round(min(1.0, score), 4)

    def _propagate(self) -> None:
        """
        Reasoning-graph propagation (full edges, material types only).

        Only MATERIAL_EDGE_TYPES edges carry weight, and each hop applies
        PROPAGATION_DECAY so long weak chains don't accumulate into strong scores.
        Edges whose effective weight falls below MIN_PATH_WEIGHT are skipped.

        importance[i] = base[i]
                      + DAMPING × Σ ( decayed_weight × importance[source] )
        """
        for _ in range(self.ITERATIONS):
            new_scores: Dict[int, float] = {}
            for pid, node in self.nodes.items():
                neighbor_contrib = 0.0
                for e in node.incoming_edges:
                    if e.source not in self.nodes:
                        continue
                    if not self._is_material_edge(e):
                        continue
                    decayed = e.weight * self.PROPAGATION_DECAY
                    if decayed < self.MIN_PATH_WEIGHT:
                        continue
                    neighbor_contrib += decayed * self.nodes[e.source].graph_importance
                new_scores[pid] = round(
                    min(1.0, node.base_importance + self.DAMPING * neighbor_contrib),
                    4,
                )
            for pid, score in new_scores.items():
                self.nodes[pid].graph_importance = score

    def _propagate_decision(self) -> None:
        """
        Decision-graph propagation using the legal weighting matrix.

        Every edge type has a legal weight from DECISION_WEIGHTS.
        Hard evidentiary links (exhibit_ref, visual_match) carry full weight.
        Procedural links (directive, page_ref) carry partial weight.
        Semantic links (semantic_sim) carry supporting weight.
        Sequential adjacency is zero-weighted and effectively excluded.

        Per-hop PROPAGATION_DECAY prevents long weak chains from accumulating.
        Edges whose decayed weight falls below MIN_PATH_WEIGHT are skipped.

        decision_importance[i] = base[i]
                               + DAMPING × Σ ( legal_weight × decay × importance[source] )
        """
        # Initialise from base seeds independently of reasoning graph
        for node in self.nodes.values():
            node.decision_importance = node.base_importance

        for _ in range(self.ITERATIONS):
            new_scores: Dict[int, float] = {}
            for pid, node in self.nodes.items():
                neighbor_contrib = 0.0
                for e in node.incoming_edges:
                    if e.source not in self.nodes:
                        continue
                    legal_w = self.DECISION_WEIGHTS.get(e.edge_type, 0.0)
                    if legal_w == 0.0:
                        continue  # sequential and unknown types have no legal weight
                    decayed = legal_w * self.PROPAGATION_DECAY
                    if decayed < self.MIN_PATH_WEIGHT:
                        continue  # path too weak to carry evidentiary weight
                    neighbor_contrib += decayed * self.nodes[e.source].decision_importance
                new_scores[pid] = round(
                    min(1.0, node.base_importance + self.DAMPING * neighbor_contrib),
                    4,
                )
            for pid, score in new_scores.items():
                self.nodes[pid].decision_importance = score

    def _is_material_edge(self, edge: ReasoningEdge) -> bool:
        """True when an edge is material enough to propagate in the reasoning graph."""
        return edge.edge_type in self.MATERIAL_EDGE_TYPES

    def _decision_weight(self, edge: ReasoningEdge) -> float:
        """Legal weight for this edge type in the decision graph (0.0 = excluded)."""
        return self.DECISION_WEIGHTS.get(edge.edge_type, 0.0)

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na < 1e-8 or nb < 1e-8:
            return 0.0
        return float(np.clip(np.dot(a, b) / (na * nb), 0.0, 1.0))

    @staticmethod
    def _color_evidence_label(vf) -> str:
        if vf is None:
            return "color content"
        if getattr(vf, 'highlight_density', 0) > 0.001:
            return "highlighted text"
        if getattr(vf, 'stamp_density', 0) > 0.0005:
            return "a colored stamp or seal"
        if getattr(vf, 'color_density', 0) > 0.03:
            return "color content"
        return "visual content"
