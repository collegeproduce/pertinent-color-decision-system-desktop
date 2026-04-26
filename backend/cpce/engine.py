"""
CPCE v6 - Color Print Classification Engine
Production-grade deterministic legal document analysis system.
Strict pipeline per specification.
"""
import time
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path

from .case_type_detector import CaseTypeDetector, CaseType
from .models import (
    CPCEConfig, DocumentContext, PageRepresentation, Signal, SignalType,
    PageRole, DecisionExplanation, VisualFeatures, SemanticFeatures
)
from .ocr_layer import OCRLayer, OCRResult
from .visual_analyzer import VisualAnalyzer
from .semantic_analyzer import SemanticAnalyzer, TFIDFEngine
from .audit_logger import AuditLogger
from .pertinence_engine import ColorPertinenceEngine
from .evidence_linker import EvidenceLinkGraph
from .visual_element_classifier import VisualElementClassifier, ELEMENT_PERTINENCE
from .legal_bert_engine import LegalBertEngine
from .reasoning_graph import LegalReasoningGraph
from .visual_instruction_graph import VisualInstructionGraph
from .arbitration_engine import LegalArbitrationEngine, ArbitrationResult
from .user_hint_engine import UserHintEngine, HintResult
from enum import Enum
from dataclasses import dataclass, field

try:
    import fitz
except ImportError:
    fitz = None


class Decision(Enum):
    """Final decision types per CPCE v6 spec."""
    COLOR = "COLOR"
    BW = "B/W"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


@dataclass
class Weights:
    """Scoring weights - must sum to 1.0."""
    wv: float = 0.30  # visual
    ws: float = 0.25  # semantic
    wr: float = 0.15  # role
    wx: float = 0.15  # reference
    wa: float = 0.10  # attention
    wc: float = 0.05  # contradiction penalty
    
    def validate(self) -> 'Weights':
        """Ensure weights sum to 1.0 within epsilon."""
        total = self.wv + self.ws + self.wr + self.wx + self.wa + self.wc
        if abs(total - 1.0) > 1e-6:
            factor = 1.0 / total
            self.wv *= factor
            self.ws *= factor
            self.wr *= factor
            self.wx *= factor
            self.wa *= factor
            self.wc *= factor
        return self


@dataclass
class DecisionResult:
    """Result object for UI compatibility — CPCE v7."""
    page_id: int
    should_use_color: bool
    confidence: float
    final_score: float
    explanation: Any = None
    is_override: bool = False
    override_reason: str = ""
    # v7 additions
    page_role: str = "unknown"
    cluster_id: Optional[int] = None
    cluster_type: str = "unknown"
    tfidf_similarity_score: float = 0.0
    visual_score: float = 0.0
    semantic_score: float = 0.0
    reasoning: str = ""
    # v8 additions
    pertinence_score: float = 0.0
    reasoning_trace: List[str] = None
    # v9 additions
    dominant_signal: str = "none"
    conflict_type: str = "none"
    # v10 additions
    decision_zone: str = "bw"            # "color" | "bw" | "review_required"
    is_review_required: bool = False
    dominant_factor_text: str = ""       # human-readable explanation
    # v12 additions
    bert_score: float = 0.0              # Legal-BERT score (0.0 if not activated)
    bert_activated: bool = False
    # v16 additions — case-aware output
    case_type: str = "unknown"                       # detected case type for this document
    important_visual_types: List[str] = None         # element types that matter for this case
    cross_page_reference: bool = False               # True if this page is cited by another
    # v17 additions
    case_visual_match_score: float = 0.0             # how well detected visuals match case type [0,1]
    # v18 additions
    propagation_boost: float = 0.0                   # cross-page score propagation contribution [0,1]
    # v19 additions — legal arbitration
    authority_weight: float = 0.0                    # legal authority weight of dominant signal [0,1]
    priority_level: str = "none"                     # human-readable legal priority label
    arbitration_justification: str = ""              # compressed 1-2 sentence arbitration justification
    conflict_resolved: bool = False                  # True if arbitration actively resolved a conflict
    # v23 additions — gate protection
    gate_forced_color: bool = False                  # True when a hard visual gate (VIG/highlight/directive/meaningful) forced COLOR

    def __post_init__(self):
        if self.reasoning_trace is None:
            self.reasoning_trace = []
        if self.important_visual_types is None:
            self.important_visual_types = []

    @property
    def decision(self) -> str:
        return "COLOR" if self.should_use_color else "B/W"


class CPCEEngine:
    """
    CPCE v6 - Deterministic Legal Document Color Classification
    
    Strict Pipeline Order:
    1. OCR / Text Extraction
    2. Visual Analysis (OpenCV)
    3. Semantic Analysis
    4. Feature Extraction
    5. Color Meaningfulness Gate
    6. Clustering (document-level)
    7. Context propagation
    8. Override check (FINAL BEFORE SCORING)
    9. Final scoring
    10. Confidence calculation
    11. Decision
    """
    
    # CPCE v6 Constants
    EPSILON = 1e-6
    DECIMAL_PLACES = 4
    
    # Override thresholds (absolute triggers)
    OVERRIDE_PHOTOS = 1
    OVERRIDE_CHARTS = 3
    OVERRIDE_HIGHLIGHT = 0.01
    OVERRIDE_STAMP = 0.005

    # Dominant signal threshold: if one signal exceeds this, it drives the decision
    DOMINANCE_THRESHOLD = 0.82

    # Case type → weight preset mapping (v16: added medical, criminal, insurance)
    _CT_WEIGHT_MAP = {
        "personal_injury":       "medical",       # wv=0.50 (visual-heavy)
        "medical":               "medical",
        "contract_dispute":      "contract",      # ws=0.50 (semantic-heavy)
        "intellectual_property": "general_litigation",
        "real_estate":           "general_litigation",
        "evidence_hearing":      "general_litigation",
        "criminal":              "medical",       # crime scene photos → visual-heavy
        "insurance":             "medical",       # damage photos → visual-heavy
        "general_litigation":    "general_litigation",
        "unknown":               "general_litigation",
    }
    
    # Decision thresholds
    COLOR_THRESHOLD = 0.5
    CONFIDENCE_THRESHOLD = 0.25
    
    def __init__(self, config: CPCEConfig = None, log_dir: str = "logs"):
        self.config = config or CPCEConfig()
        
        # Core modules only
        self.visual_analyzer = VisualAnalyzer(self.config)
        self.ocr_layer = OCRLayer()
        self.semantic_analyzer = SemanticAnalyzer(self.config)
        self.tfidf_engine = TFIDFEngine()
        self.pertinence_engine = ColorPertinenceEngine()
        self.evidence_linker = EvidenceLinkGraph()
        self.reasoning_graph = LegalReasoningGraph()
        self.visual_instruction_graph = VisualInstructionGraph()
        self.audit_logger = AuditLogger(log_dir)
        self.case_type_detector = CaseTypeDetector()
        self.visual_classifier  = VisualElementClassifier()
        self.legal_bert         = LegalBertEngine()
        self.arbitration_engine = LegalArbitrationEngine()
        self.user_hint_engine   = UserHintEngine()

        # CPCE v6 state
        self.weights = Weights().validate()
        self.case_type: str = "general_litigation"
        self.clusters: Dict[int, List[int]] = {}
        self.cluster_types: Dict[int, str] = {}
        self.global_boosts: Dict[str, float] = {}
        self._detected_case_type: str = "general_litigation"
        self._case_confidence: float = 0.5
        self._important_visual_types: List[str] = []   # v16: case-specific key element types
        # v13: set True when all pages collapse into a single cluster type
        self._global_boosts_disabled: bool = False
        
        # Document state
        self._current_pdf_path: Optional[str] = None
        self._pages_data: List[Tuple[np.ndarray, str, VisualFeatures, SemanticFeatures]] = []
    
    def initialize_case(self, case_id: str, document_path: str = None) -> None:
        """
        Initialize case for processing (desktop UI compatibility).
        """
        self.reset_case()
        # Store case info if needed later
        pass
    
    def reset_case(self):
        """Reset all state for new case. Clears memory, clustering, weights."""
        self.weights = Weights().validate()
        self.case_type = "general_litigation"
        self.clusters = {}
        self.cluster_types = {}
        self.global_boosts = {}
        self._current_pdf_path = None
        self._pages_data = []
        self._detected_case_type = "general_litigation"
        self._case_confidence = 0.5
        self._important_visual_types = []
        self._global_boosts_disabled = False
        self.reasoning_graph = LegalReasoningGraph()
        self.visual_instruction_graph = VisualInstructionGraph()

    def _clamp(self, value: float) -> float:
        """Clamp value to [0, 1] with numerical stability."""
        return round(max(0.0, min(1.0, value)), self.DECIMAL_PLACES)
    
    def _sigmoid(self, x: float) -> float:
        """Sigmoid function for semantic score normalization."""
        return self._clamp(1.0 / (1.0 + np.exp(-x)))
    
    def set_case_type(self, case_type: str):
        """Set case type with adaptive weight selection per spec."""
        self.case_type = case_type
        
        if case_type == "medical":
            self.weights = Weights(wv=0.50, ws=0.20, wr=0.10, wx=0.10, wa=0.05, wc=0.05)
        elif case_type == "contract":
            self.weights = Weights(wv=0.20, ws=0.50, wr=0.15, wx=0.10, wa=0.03, wc=0.02)
        elif case_type == "general_litigation":
            self.weights = Weights(wv=0.30, ws=0.30, wr=0.15, wx=0.15, wa=0.05, wc=0.05)
        else:
            self.weights = Weights().validate()
        
        self.weights.validate()
    
    # Case types where charts are treated as primary legal evidence
    _CHART_EVIDENCE_CASES = frozenset({
        "ip", "real_estate", "insurance", "evidence_hearing",
        "general_litigation", "unknown",
    })

    # ============================================================
    # STAGE 8: OVERRIDE SYSTEM (ABSOLUTE PRIORITY)
    # ============================================================
    def _check_override(self, visual: VisualFeatures, semantic: SemanticFeatures,
                        page_role: PageRole,
                        case_type: str = "general_litigation",
                        page_role_str: str = "unknown") -> Tuple[bool, str]:
        """
        v17: Case-aware hard override triggers.

        Photos always trigger override (case-universal evidence).
        Charts only override when case type treats charts as primary evidence.
          Contract cases: charts are NOT override-worthy unless page_role=financial_chart
          PI/Medical cases: charts are NOT override-worthy unless medical context
        Stamps and highlights trigger in any case type.
        """
        # Photos: always override regardless of case type
        if visual.photo_regions >= self.OVERRIDE_PHOTOS:
            return True, f"HIGH_RISK: evidence photos ({visual.photo_regions})"

        # Charts: case-aware gate
        if visual.chart_regions >= self.OVERRIDE_CHARTS:
            if case_type in self._CHART_EVIDENCE_CASES:
                return True, f"HIGH_RISK: evidence charts ({visual.chart_regions})"
            elif page_role_str == "financial_chart":
                # Financial chart page role validates the chart in any case type
                return True, f"HIGH_RISK: financial chart page ({visual.chart_regions} charts)"
            # else: charts exist but are not case-relevant — don't override

        if visual.highlight_density > self.OVERRIDE_HIGHLIGHT:
            return True, f"HIGH_RISK: highlighted legal text ({visual.highlight_density:.4f})"

        if visual.stamp_density > self.OVERRIDE_STAMP:
            return True, f"HIGH_RISK: official stamp/seal ({visual.stamp_density:.4f})"
        
        # Page role based
        if page_role == PageRole.EXHIBIT and visual.is_color_meaningful:
            return True, "HIGH_RISK: exhibit with meaningful color"

        # NOTE: exhibit_mentions alone is NOT a hard override — it is a pertinence boost.
        # Treating any exhibit mention as a hard trigger caused mass false COLOR decisions.
        return False, ""
    
    # ============================================================
    # STAGE 4: VISUAL SCORE (EXPLICIT FORMULA)
    # ============================================================
    def _calculate_visual_score(self, visual: VisualFeatures) -> float:
        """
        Visual score per spec:
        visual_score =
            0.35 * normalized_color_density +
            0.25 * highlight_density +
            0.20 * contour_strength +
            0.10 * stamp_density +
            0.10 * image_object_score
        """
        # normalized_color_density = clamp((color_density - 0.08) / (0.25 - 0.08), 0, 1)
        normalized_density = self._clamp(
            (visual.color_density - 0.08) / (0.25 - 0.08 + self.EPSILON)
        )
        
        # contour_strength = min(contours / 50, 1) - using entropy as proxy
        contour_strength = min(visual.entropy / 5.0, 1.0)
        
        # image_object_score = min((photos + charts) / 10, 1)
        image_object_score = min((visual.photo_regions + visual.chart_regions) / 10, 1.0)
        
        score = (
            0.35 * normalized_density +
            0.25 * visual.highlight_density +
            0.20 * contour_strength +
            0.10 * visual.stamp_density +
            0.10 * image_object_score
        )
        
        return self._clamp(score)
    
    # ============================================================
    # STAGE 5: COLOR MEANINGFULNESS (HARD GATE)
    # ============================================================
    def _is_color_meaningful(self, visual: VisualFeatures, page_role: PageRole,
                              case_type: str = "general_litigation") -> bool:
        """
        v17: Case-aware hard gate — is color meaningful FOR THIS CASE?

        Photos: always meaningful (case-universal visual evidence).
        Charts: only meaningful when the case type treats charts as primary evidence.
          - Contract cases: charts are NOT automatically meaningful (table borders,
            form grids, layout elements are detected as "charts" but are not evidence)
          - IP / Real Estate / Financial: charts ARE meaningful
        Stamps/highlights: meaningful in any case type.
        Density fallback: unchanged from v15.
        """
        if visual.photo_regions > 0:
            return True

        # v17: Charts are case-specific — not auto-meaningful in contract cases
        if visual.chart_regions >= 3:
            if case_type in self._CHART_EVIDENCE_CASES:
                return True
            # In non-chart cases, charts need additional support to be meaningful:
            # must be accompanied by meaningful color density or stamp/highlight
            if visual.color_density >= 0.08 or visual.stamp_density > 0.001:
                return True

        if visual.highlight_density > 0.005:
            return True
        if visual.stamp_density > 0.001:
            return True

        # v15: raised density floor
        if visual.color_density < 0.03:
            return False

        return visual.is_color_meaningful
    
    # ============================================================
    # STAGE 6: SEMANTIC SCORE (SIGMOID NORMALIZED)
    # ============================================================
    def _calculate_semantic_score(self, semantic: SemanticFeatures) -> float:
        """
        semantic_score = sigmoid(fuzzy_score_raw)
        Ensures output ∈ [0,1]
        """
        return self._sigmoid(semantic.fuzzy_match_score)
    
    # ============================================================
    # STAGE 6: SEMANTIC CLUSTERING
    # ============================================================
    def _perform_clustering(self, num_pages: int) -> Dict[int, List[int]]:
        """Cluster pages based on semantic and visual similarity."""
        if num_pages < 3:
            return {0: list(range(num_pages))}
        
        cluster_assignments = [-1] * num_pages
        current_cluster = 0
        
        for i in range(num_pages):
            if cluster_assignments[i] >= 0:
                continue
            
            cluster_assignments[i] = current_cluster
            
            for j in range(i + 1, num_pages):
                if cluster_assignments[j] >= 0:
                    continue
                
                if i < len(self._pages_data) and j < len(self._pages_data):
                    vi = self._pages_data[i][2]
                    vj = self._pages_data[j][2]
                    similarity = abs(vi.color_density - vj.color_density) < 0.15
                    if similarity:
                        cluster_assignments[j] = current_cluster
            
            current_cluster += 1
        
        clusters: Dict[int, List[int]] = {}
        for page_idx, cluster_id in enumerate(cluster_assignments):
            if cluster_id not in clusters:
                clusters[cluster_id] = []
            clusters[cluster_id].append(page_idx)
        
        return clusters
    
    def _determine_cluster_types(self) -> Dict[int, str]:
        """Determine cluster types based on visual evidence density."""
        cluster_types = {}
        
        for cluster_id, page_indices in self.clusters.items():
            if not page_indices:
                continue
            
            evidence_pages = 0
            for idx in page_indices:
                if idx < len(self._pages_data):
                    visual = self._pages_data[idx][2]
                    if visual.photo_regions > 0 or visual.chart_regions > 0:
                        evidence_pages += 1
            
            evidence_ratio = evidence_pages / len(page_indices)
            
            if evidence_ratio >= 0.30:
                cluster_types[cluster_id] = "evidence_cluster"
            elif evidence_ratio >= 0.10:
                cluster_types[cluster_id] = "mixed_cluster"
            else:
                cluster_types[cluster_id] = "text_cluster"
        
        return cluster_types
    
    # ============================================================
    # STAGE 7: CLUSTER DECISION PROPAGATION
    # ============================================================
    def _apply_cluster_boost(self, page_idx: int, base_score: float) -> float:
        """Apply cluster-based score adjustments."""
        if self._global_boosts_disabled:
            # v20: Cluster collapse fallback — global boosts are unreliable when all
            # pages share the same cluster type, but completely disabling boosts punishes
            # documents that consist entirely of evidence pages (e.g. a photo exhibit packet).
            # Instead, apply a scaled-down LOCAL boost based on each page's own evidence:
            # strong visual evidence on the individual page justifies a partial boost even
            # when the cluster context signal is contaminated.
            if page_idx < len(self._pages_data):
                v = self._pages_data[page_idx][2]
                _local_photos  = v.photo_regions + getattr(v, 'grayscale_regions', 0)
                _local_charts  = v.chart_regions
                _local_stamps  = 1 if v.stamp_density > 0.001 or getattr(v, 'bw_stamp_regions', 0) > 0 else 0
                _local_sig     = 1 if getattr(v, 'signature_regions', 0) > 0 else 0
                _local_evidence = _local_photos + _local_charts + _local_stamps + _local_sig
                if _local_evidence >= 2:
                    return self._clamp(base_score + 0.08)   # Reduced (vs 0.15) — lower confidence
                if _local_evidence == 1:
                    return self._clamp(base_score + 0.04)
            return base_score

        cluster_id = None
        for cid, indices in self.clusters.items():
            if page_idx in indices:
                cluster_id = cid
                break

        if cluster_id is None:
            return base_score

        cluster_type = self.cluster_types.get(cluster_id, "unknown")
        adjusted_score = base_score

        if cluster_type == "evidence_cluster":
            adjusted_score += 0.15

        if cluster_type == "evidence_cluster":
            cluster_indices = self.clusters[cluster_id]
            color_worthy = sum(
                1 for idx in cluster_indices
                if idx < len(self._pages_data) and
                (self._pages_data[idx][2].photo_regions > 0 or
                 self._pages_data[idx][2].chart_regions >= 2)
            )

            if len(cluster_indices) > 0 and color_worthy / len(cluster_indices) >= 0.50:
                adjusted_score = max(adjusted_score, 0.6)

        return self._clamp(adjusted_score)
    
    # ============================================================
    # STAGE 7: CONTEXT MEMORY RULES
    # ============================================================
    def _apply_context_memory(self, page_idx: int, base_score: float) -> float:
        """Apply context memory rules (sequence-based adjustments)."""
        adjusted_score = base_score
        
        if page_idx > 0 and page_idx < len(self._pages_data):
            prev_visual = self._pages_data[page_idx - 1][2]
            curr_visual = self._pages_data[page_idx][2]
            
            if prev_visual.is_color_meaningful:
                similar = abs(prev_visual.color_density - curr_visual.color_density) < 0.1
                if similar:
                    adjusted_score += 0.10
        
        if page_idx >= 2:
            seq_charts = all(
                self._pages_data[page_idx - i][2].chart_regions > 0
                for i in range(3)
                if page_idx - i < len(self._pages_data)
            )
            if seq_charts:
                adjusted_score = max(adjusted_score, 0.7)
        
        return self._clamp(adjusted_score)
    
    # ============================================================
    # STAGE 9: FINAL SCORING
    # ============================================================
    def _calculate_final_score(self, visual_score: float, semantic_score: float,
                              role_score: float, reference_score: float,
                              attention_score: float, visual: VisualFeatures,
                              override_triggered: bool) -> float:
        """Calculate final weighted score."""
        if override_triggered:
            return 1.0
        
        if not visual.is_color_meaningful:
            return 0.0
        
        contradiction_penalty = 0.0
        if visual_score > 0.5 and semantic_score < 0.2:
            contradiction_penalty = 0.2
        elif visual_score < 0.2 and semantic_score > 0.5:
            contradiction_penalty = 0.1
        
        final_score = (
            self.weights.wv * visual_score +
            self.weights.ws * semantic_score +
            self.weights.wr * role_score +
            self.weights.wx * reference_score +
            self.weights.wa * attention_score -
            self.weights.wc * contradiction_penalty
        )
        
        return self._clamp(final_score)
    
    # ============================================================
    # STAGE 10: CONFIDENCE CALCULATION
    # ============================================================
    def _calculate_confidence(self, visual_score: float, semantic_score: float,
                             role_score: float, reference_score: float,
                             visual: VisualFeatures, ocr_confidence: float = 0.8) -> float:
        """Calculate confidence per spec formula."""
        scores = [visual_score, semantic_score, role_score]
        std_dev = np.std(scores)
        agreement_score = self._clamp(1.0 - std_dev)
        
        contour_strength = min(visual.entropy / 5.0, 1.0)
        signal_strength = self._clamp(np.mean([
            visual.color_density,
            visual.highlight_density,
            contour_strength,
            ocr_confidence
        ]))
        
        conflict_penalty = 0.0
        if visual_score > 0.5 and semantic_score < 0.2:
            conflict_penalty = 0.3
        
        confidence = self._clamp(
            0.4 * agreement_score +
            0.3 * signal_strength +
            0.2 * reference_score +
            0.1 * (1.0 - conflict_penalty)
        )
        
        return confidence
    
    # ============================================================
    # STAGE 11: DECISION WITH FAILURE MODE
    # ============================================================
    def _make_decision(self, final_score: float, confidence: float,
                      override_triggered: bool) -> Decision:
        """Make final decision with failure mode handling."""
        if confidence < self.CONFIDENCE_THRESHOLD and not override_triggered:
            return Decision.REVIEW_REQUIRED
        
        if final_score >= self.COLOR_THRESHOLD or override_triggered:
            return Decision.COLOR
        else:
            return Decision.BW
    
    # ============================================================
    # STAGE 12: DOCUMENT-LEVEL RULES
    # ============================================================
    def _apply_document_rules(self, num_pages: int):
        """Apply global boosts based on document-level patterns."""
        if num_pages == 0:
            return

        # v20: cluster collapse — apply attenuated boosts rather than none.
        # When all pages fall into one cluster type the global signal is noisy, but
        # completely suppressing boosts misclassifies pure-evidence packets (photo exhibits)
        # by removing all document-level context.
        boost_scale = 0.40 if self._global_boosts_disabled else 1.0

        chart_pages = sum(1 for _, _, v, _ in self._pages_data if v.chart_regions > 0)
        chart_ratio = chart_pages / num_pages
        
        if chart_ratio > 0.30:
            self.global_boosts['visual'] = 0.1 * boost_scale
            self.weights.wv = min(0.6, self.weights.wv + 0.1 * boost_scale)
            self.weights.validate()

        exhibit_pages = sum(1 for _, _, _, s in self._pages_data if s.exhibit_mentions > 0)
        if exhibit_pages > 1:
            self.global_boosts['reference'] = 0.15 * boost_scale
    
    # ============================================================
    # v7: PAGE ROLE CLASSIFIER
    # ============================================================
    def _classify_page_role_v7(self, text: str, visual: VisualFeatures,
                                semantic: SemanticFeatures) -> str:
        """Classify page role based on content signals."""
        text_lower = text.lower()

        # v17: Email/correspondence detection — checked FIRST, before any visual-based roles.
        # Email headers are a reliable text signal that overrides visual noise from
        # profile avatars and social media icon bars that appear in email footers.
        email_markers = ['from:', 'to:', 'subject:', 'sent:', 'date:', 'cc:', 'bcc:']
        email_hits = sum(1 for m in email_markers if m in text_lower)
        if email_hits >= 2:
            return 'correspondence'

        if visual.photo_regions >= 1:
            if any(w in text_lower for w in ['x-ray', 'xray', 'mri', 'ct scan', 'radiograph', 'scan']):
                return 'medical_image'
            return 'evidence_photo'

        # v19: Grayscale image block = a B&W photo or scanned exhibit
        if getattr(visual, 'grayscale_regions', 0) >= 1:
            if any(w in text_lower for w in ['x-ray', 'xray', 'mri', 'ct scan', 'radiograph', 'scan']):
                return 'medical_image'
            return 'evidence_photo'

        if visual.chart_regions >= 1:
            if any(w in text_lower for w in ['financial', 'revenue', 'profit', 'loss', 'earnings', 'balance']):
                return 'financial_chart'
            return 'evidence_photo'

        if semantic.exhibit_mentions > 0 and visual.is_color_meaningful:
            return 'exhibit_page'

        # v19: B&W stamp/seal detected → signature_page regardless of color
        if (visual.stamp_density > 0.001
                or getattr(visual, 'bw_stamp_regions', 0) > 0
                or getattr(visual, 'signature_regions', 0) > 0
                or any(w in text_lower for w in ['signature', 'signed by', 'notary', 'witness', 'sworn'])):
            return 'signature_page'

        if any(w in text_lower for w in [
            'therefore', 'whereas', 'plaintiff alleges', 'defendant argues',
            'pursuant to', 'hereby', 'counsel', 'brief', 'motion', 'court'
        ]):
            return 'legal_argument'

        if any(w in text_lower for w in ['dear', 'sincerely', 'regards', 'attached hereto']):
            return 'correspondence'

        if not text.strip() or len(text.strip()) < 80:
            return 'boilerplate_text'

        return 'legal_argument'

    # ============================================================
    # v7: ADAPTIVE THRESHOLD
    # ============================================================
    def _get_adaptive_threshold(self, page_role: str) -> float:
        """Adapt decision threshold based on page role."""
        base = self.COLOR_THRESHOLD
        if page_role in ('evidence_photo', 'medical_image', 'exhibit_page'):
            return max(0.15, base - 0.25)
        if page_role == 'financial_chart':
            return max(0.20, base - 0.15)
        if page_role == 'legal_argument':
            return min(0.80, base + 0.10)
        return base

    # ============================================================
    # v7: VISUAL SCORE FORMULA
    # ============================================================
    def _calculate_visual_score_v7(self, visual: VisualFeatures,
                                    priorities: Dict[str, float] = None) -> float:
        """
        v9 visual score — region-level reasoning with case-type-aware priorities.

        Each visual region type is weighted independently by case context:
          photos     — injury photos critical in PI; product shots in IP
          charts     — financial charts critical in contract; technical diagrams in IP
          stamps     — signatures/seals critical in contract
          highlights — annotated text for any case type
          density    — general color presence (background signal)

        Priorities are provided by CaseTypeDetector.get_color_priority_for_case()
        and normalized so they always sum to 1.0 regardless of case type.
        +10% boost when visual.is_color_meaningful
        """
        if priorities is None:
            priorities = {
                'photos': 0.40, 'charts': 0.25, 'stamps': 0.15,
                'highlights': 0.10, 'general_color': 0.10
            }

        normalized_density = self._clamp(
            (visual.color_density - 0.08) / (0.25 - 0.08 + self.EPSILON)
        )
        photo_score  = min(visual.photo_regions / 1.0, 1.0)
        chart_score  = min(visual.chart_regions / 2.0, 1.0)
        stamp_score  = min(visual.stamp_density / 0.003, 1.0)
        hl_score     = min(visual.highlight_density / 0.02, 1.0)

        w_photo  = priorities.get('photos', 0.40)
        w_chart  = priorities.get('charts', 0.25)
        w_stamp  = priorities.get('stamps', 0.15)
        w_hl     = priorities.get('highlights', 0.10)
        w_dens   = priorities.get('general_color', 0.10)
        total_w  = w_photo + w_chart + w_stamp + w_hl + w_dens

        score = (
            w_photo * photo_score +
            w_chart * chart_score +
            w_stamp * stamp_score +
            w_hl    * hl_score +
            w_dens  * normalized_density
        ) / max(total_w, self.EPSILON)

        if visual.is_color_meaningful:
            score *= 1.10

        # v19: Non-color visual elements — case-type-agnostic flat boosts.
        # These are shape-based detections (sat ≈ 0) that the color pipeline never sees.
        # Weights are flat (not case-priority-weighted) because a signature on a contract
        # page is as significant as a photo on a PI page — the type of evidence is implicit.
        sig_score   = min(getattr(visual, 'signature_regions',   0) / 1.0, 1.0)
        gray_score  = min(getattr(visual, 'grayscale_regions',   0) / 1.0, 1.0)
        bws_score   = min(getattr(visual, 'bw_stamp_regions',    0) / 1.0, 1.0)
        score += 0.25 * sig_score + 0.20 * gray_score + 0.15 * bws_score

        return self._clamp(score)

    # ============================================================
    # v18: CASE VISUAL MATCH — confidence-weighted, context-proportional
    # ============================================================
    def _compute_case_visual_match(
        self, visual: VisualFeatures, text: str, page_role: str, case_type: str
    ) -> float:
        """
        v18: Confidence-weighted case visual match.

        Replaces all binary any(keyword in text) context gates with
        proportional keyword-hit-count confidence scores:

          confidence = min(1.0, hit_count / saturation_point)

        This eliminates step-function edges:
          - 1 financial term → 0.33 × max_score  (weak evidence)
          - 3+ financial terms → 1.00 × max_score (strong evidence)

        Returns [0, 1]. Used to discount visuals that don't match the case type.
          score >= 0.60 → case-relevant visuals     (no discount)
          score  < 0.30 → case-irrelevant visuals   (strong discount)
        """
        t = text.lower()

        # ── Correspondence: structural/decorative elements only ──────────
        if page_role == "correspondence":
            score = 0.0
            # Threshold raised from 0.001 → 0.020: email borders and social
            # media icon bleed read as 0.005-0.015; a real notary seal is > 0.020.
            if visual.stamp_density > 0.020:
                score += 0.40   # Notarized cover letter / stamped transmittal
            if visual.highlight_density > 0.005:
                score += 0.20   # Annotated correspondence (rare but possible)
            return min(1.0, score)

        # ── Helper: keyword hit confidence ───────────────────────────────
        def _kconf(words, saturation=3, base=0.0, per_hit=None):
            """Keyword confidence: base + hit_count * per_hit, saturated at 1.0."""
            hits = sum(1 for w in words if w in t)
            if per_hit is None:
                return min(1.0, base + hits / saturation)
            return min(1.0, base + hits * per_hit)

        # ── contract_dispute ─────────────────────────────────────────────
        if case_type == "contract_dispute":
            score = 0.0

            # Signature pages in a contract dispute are always case-critical:
            # they prove execution (or dispute non-execution) of the agreement.
            # A human paralegal would never skip a signature page regardless of
            # whether colored ink is detected — the role itself is the signal.
            if page_role == "signature_page":
                sig_conf = _kconf(
                    ['sign', 'signature', 'signed', 'execute', 'executed',
                     'witness', 'notary', 'notarized', 'date', 'party', 'parties'],
                    saturation=2,
                    base=0.35,   # base 0.35 even with zero keyword hits
                )
                return min(1.0, score + 0.75 * sig_conf)  # Early return: role is binding

            if visual.stamp_density > 0.001:
                score += 0.85       # Stamps/seals = critical contract evidence
            if visual.highlight_density > 0.005:
                score += 0.55       # Annotated contract terms
            if visual.photo_regions > 0:
                score += 0.30       # e.g., signature photos
            if visual.chart_regions > 0:
                if page_role == "financial_chart":
                    score += 0.45   # Role already confirmed financial context
                else:
                    fin_conf = _kconf([
                        'revenue', 'profit', 'financial', 'damages', 'loss', 'breach',
                        'amount', 'payment', 'cost', 'value', 'price', 'balance sheet',
                        'penalty', 'compensation', 'settlement', 'award',
                    ], saturation=3)
                    score += 0.45 * fin_conf
            return min(1.0, score)

        # ── personal_injury / medical ────────────────────────────────────
        elif case_type in ("personal_injury", "medical"):
            score = 0.0
            if visual.photo_regions > 0:
                # Medical context raises confidence; base 0.60 even without terms
                med_conf = _kconf([
                    'medical', 'diagnosis', 'treatment', 'x-ray', 'mri', 'scan',
                    'hospital', 'clinical', 'injury', 'fracture', 'trauma',
                    'plaintiff', 'accident', 'pain', 'surgery',
                ], base=0.60, per_hit=0.08)
                score += 0.95 * med_conf
            if visual.chart_regions > 0:
                chart_conf = _kconf([
                    'medical', 'diagnosis', 'treatment', 'x-ray', 'mri', 'scan',
                    'hospital', 'clinical', 'injury', 'fracture', 'trauma',
                ], saturation=3)
                score += 0.55 * chart_conf
            if visual.highlight_density > 0.005:
                score += 0.30
            if visual.stamp_density > 0.001:
                score += 0.25
            return min(1.0, score)

        # ── ip ───────────────────────────────────────────────────────────
        elif case_type == "ip":
            score = 0.0
            if visual.chart_regions > 0:
                # Technical context: more IP terms → higher confidence these are diagrams
                tech_conf = _kconf([
                    'patent', 'claim', 'invention', 'embodiment', 'prior art',
                    'technical', 'diagram', 'schematic', 'figure', 'drawing',
                    'infringement', 'specification',
                ], base=0.60, per_hit=0.07)
                score += 0.90 * tech_conf
            if visual.photo_regions > 0:
                photo_conf = _kconf([
                    'product', 'device', 'comparison', 'exhibit', 'sample', 'specimen',
                ], base=0.50, per_hit=0.10)
                score += 0.55 * photo_conf
            if visual.highlight_density > 0.005:
                score += 0.30
            return min(1.0, score)

        # ── real_estate ──────────────────────────────────────────────────
        elif case_type == "real_estate":
            score = 0.0
            if visual.photo_regions > 0:
                prop_conf = _kconf([
                    'property', 'parcel', 'lot', 'building', 'structure', 'site',
                    'premises', 'land', 'residence', 'commercial',
                ], base=0.60, per_hit=0.08)
                score += 0.88 * prop_conf
            if visual.chart_regions > 0:
                plan_conf = _kconf([
                    'survey', 'site plan', 'appraisal', 'boundary', 'easement',
                    'plat', 'map', 'dimensions', 'footprint',
                ], base=0.50, per_hit=0.10)
                score += 0.65 * plan_conf
            return min(1.0, score)

        # ── criminal / evidence_hearing ──────────────────────────────────
        elif case_type in ("criminal", "evidence_hearing"):
            score = 0.0
            if visual.photo_regions > 0:
                forensic_conf = _kconf([
                    'forensic', 'crime scene', 'evidence', 'victim', 'suspect',
                    'surveillance', 'photograph', 'exhibit', 'dna', 'fingerprint',
                ], base=0.65, per_hit=0.07)
                score += 0.95 * forensic_conf
            if visual.stamp_density > 0.001:
                score += 0.40
            if visual.chart_regions > 0:
                tl_conf = _kconf([
                    'timeline', 'chart', 'graph', 'analysis', 'data', 'report', 'table',
                ], saturation=3)
                score += 0.35 * tl_conf
            return min(1.0, score)

        # ── insurance ────────────────────────────────────────────────────
        elif case_type == "insurance":
            score = 0.0
            if visual.photo_regions > 0:
                dmg_conf = _kconf([
                    'damage', 'loss', 'property', 'accident', 'claim', 'adjuster',
                    'repair', 'vehicle', 'structure', 'flood', 'fire',
                ], base=0.60, per_hit=0.07)
                score += 0.90 * dmg_conf
            if visual.chart_regions > 0:
                val_conf = _kconf([
                    'valuation', 'appraisal', 'loss', 'estimate', 'cost',
                    'replacement', 'amount', 'depreciation',
                ], saturation=3)
                score += 0.55 * val_conf
            return min(1.0, score)

        # ── general_litigation / unknown ─────────────────────────────────
        else:
            score = 0.0
            # Legal context confidence gates all visual signals in the general case
            legal_conf = _kconf([
                'exhibit', 'evidence', 'court', 'plaintiff', 'defendant',
                'damages', 'injury', 'contract', 'breach', 'testimony',
            ], base=0.50, per_hit=0.05)
            if visual.photo_regions > 0:
                score += 0.75 * legal_conf
            if visual.chart_regions > 0:
                score += 0.60 * legal_conf
            if visual.stamp_density > 0.001:
                score += 0.50
            if visual.highlight_density > 0.005:
                score += 0.40
            return min(1.0, score)

    # ============================================================
    # v8: SEMANTIC SCORE — multi-signal, robust to sparse TF-IDF
    # ============================================================

    # Generic high-value legal/medical keywords (PI-focused baseline)
    _HIGH_VALUE_KEYWORDS = [
        'exhibit', 'evidence', 'photograph', 'photo', 'image', 'picture',
        'x-ray', 'xray', 'mri', 'ct scan', 'radiograph', 'scan', 'ultrasound',
        'medical', 'injury', 'wound', 'trauma', 'blood', 'surgical',
        'chart', 'graph', 'diagram', 'figure', 'financial', 'damages',
    ]
    # Medium-value legal process keywords
    _MED_VALUE_KEYWORDS = [
        'plaintiff', 'defendant', 'court', 'testimony', 'affidavit',
        'contract', 'agreement', 'motion', 'brief', 'deposition', 'witness',
        'liability', 'negligence', 'breach', 'statute', 'regulation',
    ]

    # v17: Case-specific high-value keywords — fixes dead TF-IDF for non-PI cases
    _CASE_HIGH_VALUE_KEYWORDS: Dict[str, List[str]] = {
        "contract_dispute": [
            'signature', 'signed', 'notarized', 'stamp', 'seal', 'executed',
            'breach', 'default', 'termination', 'obligation', 'warranty',
            'payment', 'indemnity', 'liability', 'agreement', 'contract',
            'indemnification', 'confidentiality', 'non-disclosure', 'clause',
        ],
        "personal_injury": [
            'injury', 'injured', 'x-ray', 'xray', 'mri', 'ct scan', 'medical',
            'hospital', 'surgery', 'treatment', 'diagnosis', 'fracture', 'wound',
            'trauma', 'accident', 'damages', 'pain', 'suffering', 'negligence',
        ],
        "medical": [
            'x-ray', 'xray', 'mri', 'ct scan', 'radiograph', 'ultrasound',
            'medical', 'clinical', 'diagnosis', 'treatment', 'surgery', 'biopsy',
            'pathology', 'radiology', 'specimen', 'laboratory', 'patient',
        ],
        "ip": [
            'patent', 'trademark', 'infringement', 'claim', 'invention',
            'technical', 'diagram', 'drawing', 'prior art', 'embodiment',
            'design', 'innovation', 'copyright', 'trade secret', 'patent claim',
        ],
        "real_estate": [
            'property', 'real estate', 'deed', 'title', 'survey', 'boundary',
            'appraisal', 'foreclosure', 'mortgage', 'parcel', 'easement',
            'site plan', 'floor plan', 'photograph', 'inspection',
        ],
        "criminal": [
            'crime scene', 'forensic', 'fingerprint', 'dna', 'surveillance',
            'defendant', 'prosecution', 'evidence', 'photograph', 'ballistics',
            'arrest', 'conviction', 'testimony', 'witness', 'chain of custody',
        ],
        "insurance": [
            'damage', 'claim', 'adjuster', 'photograph', 'loss', 'appraisal',
            'repair', 'total loss', 'coverage', 'accident', 'property damage',
            'bodily injury', 'medical expenses', 'policy',
        ],
        "evidence_hearing": [
            'exhibit', 'evidence', 'forensic', 'photograph', 'testimony',
            'deposition', 'witness', 'court', 'hearing', 'demonstrative',
            'chain of custody', 'marked', 'admitted',
        ],
    }

    def _score_keyword_presence(self, text: str) -> float:
        """
        Direct keyword presence score — robust against zero TF-IDF on short/sparse pages.
        High-value match = +0.12, medium = +0.05, capped at 1.0.
        """
        if not text:
            return 0.0
        t = text.lower()
        score = sum(0.12 for kw in self._HIGH_VALUE_KEYWORDS if kw in t)
        score += sum(0.05 for kw in self._MED_VALUE_KEYWORDS if kw in t)
        return min(1.0, score)

    def _score_keyword_presence_v17(self, text: str, case_type: str = "general_litigation") -> float:
        """
        v17: Case-aware keyword presence scoring.
        Fixes dead semantic signal for non-PI case types by using
        case-specific keyword sets that match the actual document vocabulary.
        """
        if not text:
            return 0.0
        t = text.lower()
        # Generic baseline
        base_score = sum(0.08 for kw in self._HIGH_VALUE_KEYWORDS if kw in t)
        base_score += sum(0.04 for kw in self._MED_VALUE_KEYWORDS if kw in t)
        # Case-specific boost — using vocabulary that matches THIS case type
        case_kws = self._CASE_HIGH_VALUE_KEYWORDS.get(case_type, [])
        case_score = sum(0.14 for kw in case_kws if kw in t)
        # Take the higher of generic baseline or case-specific score
        return min(1.0, max(base_score, case_score))

    def _calculate_semantic_score_v7(self, semantic: SemanticFeatures,
                                      tfidf_score: float, keyword_importance: float,
                                      text: str = "",
                                      case_type: str = "general_litigation") -> float:
        """
        v17 semantic score — case-aware keyword presence replaces generic baseline.

          0.20 * tfidf_similarity
          0.25 * keyword_presence_v17  (case-specific keywords fix dead TF-IDF)
          0.20 * keyword_importance    (TF-IDF top-term overlap)
          0.25 * fuzzy_score           (RapidFuzz match)
          0.10 * structural            (exhibit mentions + cross-refs)
        """
        structural = self._clamp(
            semantic.exhibit_mentions * 0.20 + semantic.cross_references * 0.08
        )
        # v17: use case-aware keyword presence (fixes contract / IP / criminal dead signal)
        kw_presence = self._score_keyword_presence_v17(text, case_type)
        fuzzy = self._clamp(semantic.fuzzy_match_score * 1.1)

        score = (
            0.20 * tfidf_score +
            0.25 * kw_presence +
            0.20 * keyword_importance +
            0.25 * fuzzy +
            0.10 * structural
        )
        return self._clamp(score)

    # ============================================================
    # v9: CASE CONTEXT ALIGNMENT
    # ============================================================
    def _compute_case_alignment_score(self, text: str) -> float:
        """
        Score [0,1] measuring how well this page's content aligns with the
        auto-detected case type.  Alignment boosts semantic relevance:
        "Does this page's text actually relate to THIS kind of case?"
        """
        if not text:
            return 0.20
        text_lower = text.lower()
        kw_map = {
            "personal_injury":       self.case_type_detector.PI_KEYWORDS,
            "contract_dispute":      self.case_type_detector.CONTRACT_KEYWORDS,
            "intellectual_property": self.case_type_detector.IP_KEYWORDS,
            "real_estate":           self.case_type_detector.REAL_ESTATE_KEYWORDS,
            "evidence_hearing":      self.case_type_detector.EVIDENCE_KEYWORDS,
        }
        keywords = kw_map.get(self._detected_case_type, self.case_type_detector.EVIDENCE_KEYWORDS)
        hits = sum(1 for kw in keywords if kw in text_lower)
        # 7 keyword hits → 1.0; 3 hits → 0.45; provides a soft case relevance signal
        return self._clamp(min(1.0, hits * 0.15))

    # ============================================================
    # v9: CONTRADICTION DETECTION
    # ============================================================
    def _detect_signal_conflicts(
        self, visual_score: float, semantic_score: float,
        pertinence_score: float, incoming_refs: int,
        page_role: str = "",
    ) -> Tuple[str, float]:
        """
        Detect contradicting signals and return a confidence penalty.

        Conflict types:
          visual_unsupported   — strong visual, no semantic corroboration
          ref_without_content  — high pertinence from refs, but page content is empty
          high_refs_low_content— many references but no visual or semantic evidence
          pertinence_semantic_gap — pertinence elevated but semantic is near zero

        Returns (conflict_type, confidence_penalty).
        """
        if visual_score > 0.65 and semantic_score < 0.15:
            # v19: Evidence photos and medical images inherently have low semantic scores —
            # the image IS the content, there is no legal text to parse.  Flagging this
            # as a "conflict" is a false positive that unfairly penalizes confirmed evidence.
            if page_role in ("evidence_photo", "medical_image"):
                pass   # Not a conflict — this is the expected signature of a photo page
            else:
                return "visual_unsupported", 0.20
        if pertinence_score > 0.65 and visual_score < 0.10 and semantic_score < 0.10:
            return "ref_without_content", 0.15
        if incoming_refs >= 2 and visual_score < 0.10 and semantic_score < 0.12:
            return "high_refs_low_content", 0.10
        if pertinence_score > 0.55 and semantic_score < 0.08:
            return "pertinence_semantic_gap", 0.12
        return "none", 0.0

    # ============================================================
    # v9: DOMINANT SIGNAL LOGIC
    # ============================================================
    def _get_dominant_signal(
        self, visual_score: float, semantic_score: float,
        pertinence_score: float, ref_strength: float,
    ) -> Tuple[str, float]:
        """
        When a single signal clearly exceeds DOMINANCE_THRESHOLD it drives the
        final score rather than being averaged with weaker signals.

        Priority order: visual → pertinence → reference → semantic
        Returns (signal_name, dominant_score) or ("none", 0.0).
        """
        if visual_score >= self.DOMINANCE_THRESHOLD:
            return "visual", visual_score
        if pertinence_score >= self.DOMINANCE_THRESHOLD:
            return "pertinence", pertinence_score
        if ref_strength >= self.DOMINANCE_THRESHOLD:
            return "reference", ref_strength
        if semantic_score >= self.DOMINANCE_THRESHOLD:
            return "semantic", semantic_score
        return "none", 0.0

    # ============================================================
    # v10: 3-ZONE PERTINENCE DECISION
    # ============================================================
    def _make_decision_v10(
        self, effective_pertinence: float, confidence: float, conflict_type: str
    ) -> Tuple[Decision, str]:
        """
        3-zone pertinence-based decision per spec:
          >= 0.75 → COLOR    (only if confidence >= 0.65; else REVIEW)
          <= 0.25 → B/W
          else    → REVIEW_REQUIRED

        v15: Confidence gating at the COLOR threshold.
        A high pertinence score with low confidence (< 0.65) means the system
        is not certain enough to commit to COLOR — route to human review instead.
        This reduces false COLOR while preserving the REVIEW_REQUIRED escalation path.

        Conflict + very low confidence always forces REVIEW_REQUIRED.
        """
        if conflict_type != "none" and confidence < 0.40:
            return Decision.REVIEW_REQUIRED, "review_required"
        if effective_pertinence >= 0.75:
            # v15: gate COLOR on confidence — uncertain high-pertinence → REVIEW
            if confidence >= 0.65:
                return Decision.COLOR, "color"
            return Decision.REVIEW_REQUIRED, "review_required"
        elif effective_pertinence <= 0.25:
            return Decision.BW, "bw"
        else:
            return Decision.REVIEW_REQUIRED, "review_required"

    # ============================================================
    # v10 (revised): HUMAN-READABLE DOMINANT FACTOR TEXT
    # ============================================================
    def _get_dominant_factor_text(
        self, dominant_factor: str, visual: VisualFeatures,
        semantic: SemanticFeatures, page_role: str,
        incoming_refs: int, effective_pertinence: float,
        should_use_color: bool, is_review: bool,
        link_info=None, prior_directive_count: int = 0,
        reasoning_chain: List[str] = None,
        gate_forced_color: bool = False,
        reasoning_trace: List[str] = None,
    ) -> str:
        """
        Paralegal-style one-liner explaining why color does or doesn't matter.
        Specific, page-aware, cross-page-aware, gate-aware.  Reads like a human wrote it.
        Updated v23: reflects VIG-COLOR, HIGHLIGHT-TEXT-COLOR, DIRECTIVE gates,
        MEANINGFUL-COLOR, WEAK-EVIDENCE-DEMOTE, and gate_forced_color protection.
        """
        trace = reasoning_trace or []

        # ── Detect which gate fired (if any) ────────────────────────
        _vig_fired        = any("VIG-COLOR" in t for t in trace)
        _hl_fired         = any("HIGHLIGHT-TEXT-COLOR" in t for t in trace)
        _dir_self_fired   = any("DIRECTIVE-SELF-COLOR" in t for t in trace)
        _dir_vis_fired    = any("DIRECTIVE-COLOR" in t and "SELF" not in t for t in trace)
        _meaningful_fired = any("MEANINGFUL-COLOR" in t for t in trace)
        _weak_demoted     = any("WEAK-EVIDENCE-DEMOTE" in t for t in trace)
        _zero_color_bw    = any("ZERO-COLOR→B/W" in t for t in trace)

        # ── Citing page context — pull 1-indexed page numbers for display ──
        citing_pages = []
        if link_info and getattr(link_info, 'referenced_by', None):
            citing_pages = [p + 1 for p in link_info.referenced_by]
        citing_str = f"page {citing_pages[0]}" if len(citing_pages) == 1 \
            else f"pages {', '.join(str(p) for p in citing_pages[:3])}"

        # ── Exhibit labels resolved on this page ───────────────────
        resolved = getattr(link_info, 'resolved_exhibits', []) if link_info else []
        exhibit_str = f"Exhibit {resolved[0]}" if resolved else "a referenced exhibit"

        # ── VIG source/target pages from chain ─────────────────────
        vig_chain_top = None
        for t in trace:
            if "VIG-COLOR" in t and "instructed" in t:
                vig_chain_top = t
                break
        if not vig_chain_top and reasoning_chain:
            for c in reasoning_chain:
                if "instructed" in c and "pages" in c.lower():
                    vig_chain_top = c
                    break

        # ── Visual element summary ─────────────────────────────────
        sig_regions  = getattr(visual, 'signature_regions', 0)
        bw_stamps    = getattr(visual, 'bw_stamp_regions', 0)
        gray_regions = getattr(visual, 'grayscale_regions', 0)
        hl_density   = getattr(visual, 'highlight_density', 0.0)
        hl_text_dens = getattr(visual, 'highlighted_text_density', 0.0)
        ve_score     = getattr(visual, 'visual_evidence_score', 0.0)

        # ── Gate-specific explanations (highest priority) ──────────
        if _vig_fired and should_use_color:
            src = vig_chain_top or "a prior page"
            return (
                f"A visual instruction from {src} directed the reader here — "
                f"this page contains the referenced visual content (hl={hl_density:.4f}, "
                f"color density={visual.color_density:.3f})"
            )

        if _hl_fired and should_use_color:
            pct = round(hl_density * 100, 2)
            return (
                f"Text highlighting confirmed on this page ({pct}% highlight density) — "
                "highlighted annotations are the evidentiary content and must print in color"
            )

        if _dir_self_fired and should_use_color:
            feats = ", ".join(getattr(link_info, 'directive_self_features', []) or []) or "a visual element"
            return (
                f"This page directs readers to {feats} that is visually confirmed here "
                f"(evidence score={ve_score:.3f}) — both the instruction and the evidence are on this page"
            )

        if _dir_vis_fired and should_use_color:
            feats = ", ".join(getattr(link_info, 'directive_visual_features', []) or []) or "a visual element"
            src_pg = citing_pages[0] if citing_pages else "a prior page"
            return (
                f"Page {src_pg} issued a color/visual directive — "
                f"the {feats} it referenced is confirmed on this page"
            )

        if _meaningful_fired and should_use_color:
            return (
                f"Meaningful colored annotation detected in the body of this page "
                f"(color density={visual.color_density:.3f}, "
                f"highlight signal confirmed) — color content is substantive, not decorative"
            )

        if _weak_demoted and not should_use_color:
            return (
                f"Cross-reference only — no visual backing (visual score={ve_score:.3f}, "
                f"TF-IDF low) — color adds nothing for printing; cited by {citing_str} "
                "but no confirming evidence on this page"
            )

        if _zero_color_bw and not should_use_color:
            return (
                f"No measurable color content (density={visual.color_density:.4f}) — "
                "page is effectively monochrome and safe to print in B/W"
            )

        # ── Standard dominant-factor explanations ──────────────────
        if dominant_factor == "photo_evidence":
            if page_role == "medical_image":
                return (
                    f"Medical imaging detected ({visual.photo_regions} scan region(s)) — "
                    "loses diagnostic accuracy when printed in black and white"
                )
            return (
                f"{visual.photo_regions} evidence photo(s) confirmed "
                f"(evidence score={ve_score:.3f}) — "
                "photos must print in color to preserve evidentiary detail"
            )

        if dominant_factor == "chart_evidence":
            return (
                f"{visual.chart_regions} data visualization(s) detected — "
                "color distinguishes the data series; without it the chart becomes unreadable"
            )

        if dominant_factor == "stamp":
            return (
                f"Official stamp or seal present (density={visual.stamp_density:.4f}) — "
                "color is required to confirm authenticity and distinguish genuine seals from copies"
            )

        if dominant_factor == "highlight":
            pct = round(hl_density * 100, 2)
            if hl_text_dens > 0.0005:
                return (
                    f"Highlighted text confirmed ({pct}% area, text density={hl_text_dens:.4f}) — "
                    "the highlighting is the annotation; printing B/W erases it"
                )
            return (
                f"Highlighted area detected ({pct}% density) — "
                "the annotation itself is the evidence and must print in color"
            )

        if dominant_factor == "visual_attr_confirmed":
            if hl_density > 0.001:
                attr = f"highlighted text (density={hl_density:.4f})"
            elif visual.stamp_density > 0.0005:
                attr = f"a colored stamp or marking (density={visual.stamp_density:.4f})"
            elif visual.color_density > 0.015:
                attr = f"color annotation (density={visual.color_density:.3f})"
            else:
                attr = "color content"
            if prior_directive_count > 0 and citing_pages:
                return (
                    f"Page {citing_pages[0]} directed the reader here and "
                    f"the {attr} it described is visually confirmed on this page"
                )
            return f"The {attr} referenced in the text is visually confirmed on this page"

        if dominant_factor == "cross_page_context":
            chain_top = (reasoning_chain[0] if reasoning_chain else None)
            if chain_top:
                return chain_top
            if prior_directive_count > 0 and citing_pages:
                return (
                    f"Page {citing_pages[0]} issued a directive pointing here — "
                    "this is the instructed reference page"
                )
            if citing_pages:
                return (
                    f"Cited by {citing_str} in this document — "
                    "cross-page evidence linkage establishes legal significance"
                )
            return "Prior pages establish context that makes this page legally significant"

        if dominant_factor == "cross_reference":
            chain_top = (reasoning_chain[0] if reasoning_chain else None)
            if chain_top:
                return chain_top
            if citing_pages:
                return (
                    f"Cited by {citing_str} — "
                    "another page in this document identifies this one as evidence"
                )
            return f"Referenced by {incoming_refs} other page(s) — carries cross-page evidentiary weight"

        if dominant_factor == "exhibit_resolution":
            if citing_pages:
                return (
                    f"This page is {exhibit_str}, first introduced on {citing_str} — "
                    "exhibits must print in their original color"
                )
            return f"This page resolves {exhibit_str} referenced elsewhere in the document"

        if dominant_factor.startswith("page_role:"):
            role = dominant_factor.split(":", 1)[1]
            labels = {
                "medical_image":   "medical imaging (X-ray / MRI / CT scan)",
                "evidence_photo":  "photographic evidence",
                "financial_chart": "financial data visualization",
                "exhibit_page":    "a legal exhibit",
                "signature_page":  "a signature or authentication page",
            }
            return f"Classified as {labels.get(role, role)} — color is legally required for this document type"

        # ── Fallback labels ────────────────────────────────────────
        if sig_regions > 0 or bw_stamps > 0:
            return "Handwritten signature or official seal detected — authenticity depends on color fidelity"
        if gray_regions > 0:
            return "Grayscale photographic content detected — likely a scanned exhibit or documentary photo"

        if is_review:
            return (
                f"Color signals are mixed (pertinence={effective_pertinence:.2f}, "
                f"ve={ve_score:.3f}) — manual review needed before final print decision"
            )
        if not should_use_color:
            return (
                f"No photos, highlights, stamps, or confirmed cross-references found "
                f"(color density={visual.color_density:.4f}, ve={ve_score:.3f}) — "
                "color adds nothing here; safe to print in B/W"
            )
        return "Color content confirmed by multiple signals — this page requires color printing"

    # ============================================================
    # v13: ROLE-AWARE CLUSTERING (replaces pure TF-IDF KMeans)
    # ============================================================

    # Role → cluster type mapping
    _ROLE_CLUSTER_MAP: Dict[str, str] = {
        "evidence_photo":    "evidence_cluster",
        "medical_image":     "evidence_cluster",
        "financial_chart":   "evidence_cluster",
        "exhibit_page":      "evidence_cluster",
        "legal_argument":    "text_cluster",
        "signature_page":    "text_cluster",
        "boilerplate_text":  "text_cluster",
        "correspondence":    "text_cluster",
        # everything else → mixed_cluster
    }
    _CLUSTER_IDS: Dict[str, int] = {
        "evidence_cluster": 0,
        "text_cluster":     1,
        "mixed_cluster":    2,
    }

    def _assign_role_aware_clusters(
        self,
        texts: List[str],
        visual_features_list: List,
        semantic_features_list: List,
    ) -> Tuple[Dict[int, List[int]], Dict[int, str]]:
        """
        v13 (revised): Assign clusters by page role + semantic promotion.

        Guarantees minimum 3 cluster types exist when content is diverse.
        Detects cluster collapse (all pages → same type) and sets
        self._global_boosts_disabled = True to prevent false contamination.

        v20 revision: pages classified as text_cluster are promoted to
        mixed_cluster when they carry cross-page semantic signals:
          - exhibit_mentions > 0  (page references or declares an exhibit)
          - incoming references from the EvidenceLinkGraph (cited by other pages)
          - meaningful color signals (stamps, highlights) even without photos

        This prevents the common collapse scenario in text-heavy legal documents
        where every page gets 'legal_argument' → 'text_cluster', eliminating
        all cluster diversity and disabling global boosts.

        Returns (clusters dict, cluster_types dict).
        """
        page_cluster_types: List[str] = []
        for idx, (text, visual, semantic) in enumerate(
            zip(texts, visual_features_list, semantic_features_list)
        ):
            raw_role = self._classify_page_role_v7(text, visual, semantic)
            ctype = self._ROLE_CLUSTER_MAP.get(raw_role, "mixed_cluster")

            # ── Mixed-cluster promotion ─────────────────────────────
            # Promote text_cluster pages that carry cross-page semantic
            # or color signals to mixed_cluster so the system retains
            # meaningful cluster diversity on text-heavy documents.
            if ctype == "text_cluster":
                # Promotion gate 1: exhibit or cross-ref mentions
                if semantic.exhibit_mentions > 0 or semantic.cross_references > 0:
                    ctype = "mixed_cluster"
                # Promotion gate 2: meaningful color markers (stamps, highlights)
                elif (visual.stamp_density > 0.0005
                      or visual.highlight_density > 0.001
                      or getattr(visual, 'bw_stamp_regions', 0) > 0
                      or getattr(visual, 'signature_regions', 0) > 0):
                    ctype = "mixed_cluster"
                # Promotion gate 3: page has an incoming reference from the link graph
                elif self.evidence_linker and idx in getattr(self.evidence_linker, '_links', {}):
                    link = self.evidence_linker._links[idx]
                    if link.incoming_ref_count > 0 or link.directive_count > 0:
                        ctype = "mixed_cluster"

            page_cluster_types.append(ctype)

        # Build clusters: {cluster_id: [page_indices]}
        clusters: Dict[int, List[int]] = {0: [], 1: [], 2: []}
        for idx, ctype in enumerate(page_cluster_types):
            clusters[self._CLUSTER_IDS[ctype]].append(idx)

        # Remove empty buckets
        clusters = {cid: pages for cid, pages in clusters.items() if pages}

        cluster_types: Dict[int, str] = {
            0: "evidence_cluster",
            1: "text_cluster",
            2: "mixed_cluster",
        }

        # Detect collapse: if only 1 unique cluster type, disable global boosts
        unique_types = set(page_cluster_types)
        self._global_boosts_disabled = (len(unique_types) <= 1)
        if self._global_boosts_disabled:
            print(f"  WARNING: Cluster collapse — all {len(texts)} pages in "
                  f"'{next(iter(unique_types))}' — global boosts DISABLED")

        return clusters, cluster_types

    # ============================================================
    # v7: TF-IDF CLUSTERING
    # ============================================================
    def _perform_tfidf_clustering(self, texts: List[str]) -> Dict[int, List[int]]:
        """Cluster pages using TF-IDF KMeans (falls back to density clustering)."""
        labels = self.tfidf_engine.cluster_by_tfidf(texts)
        clusters: Dict[int, List[int]] = {}
        for page_idx, label in enumerate(labels):
            clusters.setdefault(label, []).append(page_idx)
        return clusters

    def _get_cluster_importance(self, cluster_id: Optional[int]) -> float:
        """Return a [0,1] importance score for a cluster."""
        if cluster_id is None:
            return 0.3
        ctype = self.cluster_types.get(cluster_id, 'text_cluster')
        if ctype == 'evidence_cluster':
            return 0.85
        if ctype == 'mixed_cluster':
            return 0.55
        return 0.25

    # ============================================================
    # v23: LEGAL REASONING GENERATOR — paralegal-style, gate-aware
    # ============================================================
    def _generate_legal_reasoning(
        self, page_id: int, decision: bool, page_role: str,
        visual: VisualFeatures, semantic: SemanticFeatures,
        visual_score: float, semantic_score: float,
        cluster_type: str, cluster_importance: float,
        override_triggered: bool, override_reason: str,
        tfidf_top_terms: List[str], tfidf_score: float,
        dominant_factor_text: str = "",
        is_review_required: bool = False,
        link_info=None, prior_directive_count: int = 0,
        reasoning_chain: List[str] = None,
        reasoning_trace: List[str] = None,
        gate_forced_color: bool = False,
    ) -> str:
        """
        Paralegal-style narrative: 3-5 sentences, specific, human-readable.
        Answers "why does / doesn't color matter here?" with precise references
        to visual features, cross-page relationships, and gate decisions.
        Updated v23: reflects all gates (VIG, HIGHLIGHT, DIRECTIVE, MEANINGFUL,
        WEAK-EVIDENCE-DEMOTE) and their specific evidence values.
        """
        display_page = page_id + 1

        # ── Gate detection from trace ──────────────────────────────
        trace = reasoning_trace or []
        _vig_fired        = any("VIG-COLOR" in t for t in trace)
        _hl_fired         = any("HIGHLIGHT-TEXT-COLOR" in t for t in trace)
        _dir_self_fired   = any("DIRECTIVE-SELF-COLOR" in t for t in trace)
        _dir_vis_fired    = any("DIRECTIVE-COLOR" in t and "SELF" not in t for t in trace)
        _meaningful_fired = any("MEANINGFUL-COLOR" in t for t in trace)
        _weak_demoted     = any("WEAK-EVIDENCE-DEMOTE" in t for t in trace)
        _hint_applied     = any("Paralegal hint" in t or "HINT" in t for t in trace)
        _dir_self_skipped = any("DIRECTIVE-SELF-COLOR skipped" in t for t in trace)

        # ── Cross-page context ─────────────────────────────────────
        citing_pages      = []
        resolved_exhibits = []
        directive_pages   = []
        if link_info:
            citing_pages      = [p + 1 for p in getattr(link_info, 'referenced_by', [])]
            resolved_exhibits = getattr(link_info, 'resolved_exhibits', [])
            directive_pages   = [p + 1 for p in getattr(link_info, 'directive_sources', [])]
        citing_str   = (f"page {citing_pages[0]}" if len(citing_pages) == 1
                        else f"pages {', '.join(str(p) for p in citing_pages[:3])}")
        exhibit_str  = (f"Exhibit {resolved_exhibits[0]}" if resolved_exhibits
                        else "the referenced exhibit")
        dir_src_str  = (f"page {directive_pages[0]}" if directive_pages
                        else (f"page {citing_pages[0]}" if citing_pages else "a prior page"))

        # ── Visual feature shorthands ──────────────────────────────
        hl_density   = getattr(visual, 'highlight_density', 0.0)
        hl_txt       = getattr(visual, 'highlighted_text_density', 0.0)
        ve_score     = getattr(visual, 'visual_evidence_score', 0.0)
        col_dens     = visual.color_density
        n_photos     = visual.photo_regions
        n_charts     = visual.chart_regions
        n_stamps     = getattr(visual, 'stamp_density', 0.0)
        sig_regions  = getattr(visual, 'signature_regions', 0)
        bw_stamps    = getattr(visual, 'bw_stamp_regions', 0)

        # ── Role labels ────────────────────────────────────────────
        _ROLE_LABELS = {
            "medical_image":   "medical imaging",
            "evidence_photo":  "photographic evidence",
            "financial_chart": "financial data visualization",
            "exhibit_page":    "a legal exhibit",
            "signature_page":  "a signature page",
            "legal_argument":  "legal argument",
            "correspondence":  "correspondence",
            "boilerplate_text":"boilerplate / standard text",
            "unknown":         "general content",
        }
        role_label = _ROLE_LABELS.get(page_role, page_role.replace("_", " "))

        # ── VIG source line extraction ─────────────────────────────
        vig_src_line = None
        for t in trace:
            if "VIG-COLOR" in t and "Page" in t:
                vig_src_line = t
                break

        parts: List[str] = []

        # ==============================================================
        # SECTION A: Opening — decision + primary driver
        # ==============================================================
        decision_label = (
            "REVIEW REQUIRED" if is_review_required
            else ("COLOR" if decision else "MONOCHROME (B/W)")
        )

        if is_review_required:
            parts.append(
                f"Page {display_page} — {decision_label}.  "
                f"This {role_label} page has conflicting color signals "
                f"(pertinence={visual_score + semantic_score:.2f}, "
                f"color density={col_dens:.3f}, ve={ve_score:.3f}) that cannot be "
                "resolved automatically — a paralegal should visually inspect this page "
                "before the print run."
            )

        elif decision and gate_forced_color:
            # Gate-forced COLOR — explain which gate and why
            if _vig_fired:
                src_desc = vig_src_line or f"a visual instruction from {dir_src_str}"
                parts.append(
                    f"Page {display_page} — COLOR.  "
                    f"This page was identified by a cross-page visual instruction: "
                    f"{src_desc.strip()}.  "
                    f"The visual feature referenced in that instruction is confirmed here "
                    f"(highlight density={hl_density:.4f}, color density={col_dens:.3f})."
                )
            elif _hl_fired:
                pct = round(hl_density * 100, 2)
                parts.append(
                    f"Page {display_page} — COLOR.  "
                    f"This {role_label} page contains highlighted text covering {pct}% "
                    f"of its area (text density={hl_txt:.4f}).  "
                    "Highlighting is the annotation — it is the evidence itself, and "
                    "printing in black and white would erase it entirely."
                )
            elif _dir_self_fired:
                feats = ", ".join(getattr(link_info, 'directive_self_features', []) or []) or "color content"
                parts.append(
                    f"Page {display_page} — COLOR.  "
                    f"This {role_label} page contains a directive to the reader pointing "
                    f"to {feats}, and that same feature is visually confirmed on this page "
                    f"(evidence score={ve_score:.3f}, color density={col_dens:.3f}).  "
                    "Both the instruction and the evidence it references are present here."
                )
            elif _dir_vis_fired:
                feats = ", ".join(getattr(link_info, 'directive_visual_features', []) or []) or "color content"
                parts.append(
                    f"Page {display_page} — COLOR.  "
                    f"{dir_src_str.capitalize()} issued a directive pointing to {feats}, "
                    f"and that feature is visually confirmed on this page "
                    f"(evidence score={ve_score:.3f}, color density={col_dens:.3f})."
                )
            elif _meaningful_fired:
                parts.append(
                    f"Page {display_page} — COLOR.  "
                    f"This {role_label} page contains meaningful colored annotations in its body "
                    f"(color density={col_dens:.3f}) with a confirmed highlight signal "
                    f"(hl={hl_density:.4f}).  "
                    "The color is substantive — it marks annotated or emphasized content — "
                    "not decorative layout."
                )
            else:
                # gate_forced_color but no specific gate detected — override or other hard gate
                parts.append(
                    f"Page {display_page} — COLOR.  "
                    f"{dominant_factor_text or 'Color printing required by a confirmed visual gate.'}"
                )

        elif decision and not gate_forced_color:
            # Scored COLOR (pertinence path)
            parts.append(
                f"Page {display_page} — COLOR.  "
                f"{dominant_factor_text or 'Color content confirmed by scoring signals.'}"
            )

        else:
            # B/W decision
            if _weak_demoted:
                parts.append(
                    f"Page {display_page} — MONOCHROME.  "
                    f"This {role_label} page is cited by {citing_str} but has no confirming "
                    f"visual evidence (evidence score={ve_score:.3f}, "
                    f"color density={col_dens:.4f}, TF-IDF={tfidf_score:.3f}).  "
                    "A cross-reference alone is not sufficient to require color printing — "
                    "color would add nothing to the printed output."
                )
            else:
                parts.append(
                    f"Page {display_page} — MONOCHROME.  "
                    f"{dominant_factor_text or 'No confirmed color evidence on this page.'}"
                )

        # ==============================================================
        # SECTION B: Visual evidence specifics (for COLOR / REVIEW pages)
        # ==============================================================
        if decision or is_review_required:
            vis_details: List[str] = []
            if n_photos > 0:
                vis_details.append(f"{n_photos} photo region(s)")
            if n_charts > 0:
                vis_details.append(f"{n_charts} chart/graph region(s)")
            if hl_density > 0.001:
                vis_details.append(f"highlighted text ({round(hl_density*100,2)}% area)")
            if n_stamps > 0.0005:
                vis_details.append(f"stamp/seal (density={n_stamps:.4f})")
            if col_dens > 0.015 and not vis_details:
                vis_details.append(f"color annotation (density={col_dens:.3f})")
            if sig_regions > 0:
                vis_details.append(f"{sig_regions} signature region(s)")
            if bw_stamps > 0:
                vis_details.append(f"{bw_stamps} B/W stamp(s)")

            if vis_details and not _hl_fired and not _vig_fired:
                parts.append(
                    f"Visual evidence on this page: {'; '.join(vis_details)}."
                )

        # ==============================================================
        # SECTION C: Cross-page relationships
        # ==============================================================
        if citing_pages and not _weak_demoted:
            if prior_directive_count > 0:
                parts.append(
                    f"{dir_src_str.capitalize()} issued a directive that leads the reader "
                    "to this page as the instructed reference."
                )
            elif resolved_exhibits:
                parts.append(
                    f"This page resolves {exhibit_str}, "
                    f"first referenced on {citing_str}."
                )
            elif not (gate_forced_color and (_vig_fired or _dir_vis_fired)):
                # Don't double-mention if a directive gate already described the cross-page link
                parts.append(
                    f"This page is cited by {citing_str} elsewhere in the document."
                )

        # ==============================================================
        # SECTION D: Directive skip / BERT / override notes
        # ==============================================================
        if _dir_self_skipped:
            parts.append(
                f"Note: a directive on this page referenced a visual feature, but no "
                f"confirming visual evidence was found (evidence score={ve_score:.3f} < 0.10) — "
                "the directive likely points to an attachment or external document."
            )

        if override_triggered:
            parts.append(
                f"Legal override applied: {override_reason}."
            )

        if _hint_applied:
            for t in trace:
                if "Paralegal hint" in t or ("hint" in t.lower() and "applied" in t.lower()):
                    parts.append(f"Paralegal annotation: {t.strip()}.")
                    break

        # ==============================================================
        # SECTION E: Key terms (text-heavy pages only)
        # ==============================================================
        if (tfidf_top_terms
                and page_role in ("legal_argument", "correspondence", "unknown")
                and not is_review_required
                and tfidf_score > 0.05):
            terms = ", ".join(tfidf_top_terms[:3])
            parts.append(f"Key legal terms on this page: {terms}.")

        # ==============================================================
        # SECTION F: Closing action note
        # ==============================================================
        if is_review_required:
            parts.append(
                "Action: flag this page for attorney review before finalizing the print order."
            )
        elif decision:
            if gate_forced_color:
                parts.append("Print this page in color — the gate-confirmed evidence requires it.")
            else:
                parts.append("Include in color print run.")
        else:
            if not _weak_demoted:
                parts.append("Safe to include in the monochrome (B/W) print run.")

        return "  ".join(parts)

    # ============================================================
    # MAIN PIPELINE: process_document
    # ============================================================
    def process_document(
        self,
        images:     List[np.ndarray],
        pdf_path:   str = None,
        page_hints: Dict[int, str] = None,
    ) -> List[Dict]:
        """
        CPCE v8 pipeline — Color Pertinence Engine + Evidence Linking + Reasoning Traces.

        Pipeline Order:
        1.  OCR / Text Extraction
        1b. TF-IDF Fit on document texts
        2.  Visual Analysis (OpenCV)
        3.  Semantic Analysis + TF-IDF scores
        4.  Evidence Link Graph (cross-page references, exhibit resolution)
        5.  Color Meaningfulness Gate
        6.  TF-IDF Clustering (document-level)
        7.  Context propagation + cluster importance
        8.  Per-page: Pertinence Engine (primary decision driver)
                       → contextual override validation
                       → adaptive threshold
                       → multi-step reasoning trace
        9.  Confidence calculation
        10. Decision
        11. Document-level rules
        """
        self.reset_case()
        self.user_hint_engine.set_hints(page_hints)
        self._current_pdf_path = pdf_path

        num_pages = len(images)
        print(f"\n{'='*60}")
        print(f"CPCE v18 Processing Document: {num_pages} pages")
        print(f"{'='*60}\n")

        # ============================================================
        # STAGE 1: OCR / Text Extraction
        # ============================================================
        print("STAGE 1: Text Extraction...")
        texts = []
        # Open the PDF once for all pages instead of reopening per page
        _pdf_doc = None
        if pdf_path and fitz:
            try:
                _pdf_doc = fitz.open(pdf_path)
            except Exception:
                _pdf_doc = None
        for i, img in enumerate(images):
            text = ""
            if _pdf_doc and i < len(_pdf_doc):
                try:
                    text = _pdf_doc.load_page(i).get_text()
                except Exception:
                    text = ""
            if not text:
                ocr_result = self.ocr_layer.extract_text(img)
                text = ocr_result.text if ocr_result else ""
            texts.append(text)
        if _pdf_doc:
            _pdf_doc.close()
        print(f"  Extracted text from {len(texts)} pages")

        # ============================================================
        # STAGE 1b: TF-IDF Fit (document-level)
        # ============================================================
        print("\nSTAGE 1b: TF-IDF Fit...")
        self.tfidf_engine.fit_document(texts)
        print("  TF-IDF vectorizer fitted on document corpus")

        # ============================================================
        # STAGE 2: Visual Analysis (OpenCV)
        # ============================================================
        print("\nSTAGE 2: Visual Analysis...")
        visual_features_list = []
        for i, img in enumerate(images):
            vf = self.visual_analyzer.analyze(img, PageRole.UNKNOWN)
            visual_features_list.append(vf)
            print(f"  Page {i}: photos={vf.photo_regions}, charts={vf.chart_regions}, "
                  f"density={vf.color_density:.3f}, meaningful={vf.is_color_meaningful}")

        # ============================================================
        # STAGE 3: Semantic Analysis + TF-IDF Scores
        # ============================================================
        print("\nSTAGE 3: Semantic Analysis (TF-IDF + fuzzy)...")
        semantic_features_list = []
        tfidf_scores: List[float] = []
        tfidf_top_terms_list: List[List[str]] = []
        keyword_importance_list: List[float] = []

        for i, text in enumerate(texts):
            sf = self.semantic_analyzer.analyze(text)
            semantic_features_list.append(sf)
            tfidf_score = self.tfidf_engine.get_legal_similarity(text)
            tfidf_top_terms = self.tfidf_engine.get_top_terms(text, 5)
            kw_importance = self.tfidf_engine.get_keyword_importance(text)
            tfidf_scores.append(tfidf_score)
            tfidf_top_terms_list.append(tfidf_top_terms)
            keyword_importance_list.append(kw_importance)
            print(f"  Page {i}: tfidf={tfidf_score:.3f}, kw={kw_importance:.3f}, "
                  f"exhibits={sf.exhibit_mentions}, terms={tfidf_top_terms[:3]}")

        # ============================================================
        # STAGE 2b: Visual Element Classification (v10)
        # ============================================================
        print("\nSTAGE 2b: Visual Element Classification...")
        for i, img in enumerate(images):
            vf = visual_features_list[i]
            page_text = texts[i] if i < len(texts) else ""
            classifications, ve_score = self.visual_classifier.classify_page_regions(
                vf.photo_region_data, vf.chart_region_data, img, page_text
            )
            vf.region_classifications = classifications
            vf.visual_evidence_score  = ve_score
            if classifications:
                type_summary = ", ".join(
                    f"{c.element_type}({c.confidence:.2f})" for c in classifications
                )
                print(f"  Page {i}: {len(classifications)} region(s) → {type_summary} "
                      f"| evidence_score={ve_score:.3f}")
            else:
                print(f"  Page {i}: no classified regions")

        # Store for clustering and context
        self._pages_data = list(zip(images, texts, visual_features_list, semantic_features_list))

        # ============================================================
        # STAGE 3b: Case Type Auto-Detection (v9)
        # ============================================================
        print("\nSTAGE 3b: Case Type Detection...")
        detected_case, case_conf = self.case_type_detector.detect_case_type(texts)
        self._detected_case_type = detected_case
        self._case_confidence = case_conf
        weight_preset = self._CT_WEIGHT_MAP.get(detected_case, "general_litigation")
        self.set_case_type(weight_preset)
        # v16: switch BERT reference to case-appropriate domain text
        self.legal_bert.set_case_reference(detected_case)
        # v16: cache important/ignore visual types for output annotation
        self._important_visual_types = self.case_type_detector.get_important_visual_types(detected_case)
        print(f"  Detected case type: '{detected_case}' (conf {case_conf:.2%}) → "
              f"weights='{weight_preset}', key visuals={self._important_visual_types}")

        # ============================================================
        # STAGE 4: Evidence Link Graph (v8 — cross-page references)
        # ============================================================
        print("\nSTAGE 4: Evidence Link Graph...")
        self.evidence_linker = EvidenceLinkGraph()
        self.evidence_linker.build(texts, semantic_features_list, visual_features_list)
        link_summary = self.evidence_linker.summary()
        print(f"  Links: {link_summary['total_incoming_refs']} cross-refs, "
              f"{link_summary['resolved_exhibit_pages']} exhibit pages, "
              f"{link_summary['total_forward_directives']} forward directives, "
              f"exhibits={list(link_summary['exhibit_locations'].keys())[:5]}")

        # ============================================================
        # STAGE 4b: Cross-Page Legal Reasoning Graph
        # ============================================================
        print("\nSTAGE 4b: Reasoning Graph...")
        _page_roles_for_graph = [
            self._classify_page_role_v7(t, v, s)
            for t, v, s in zip(texts, visual_features_list, semantic_features_list)
        ]
        self.reasoning_graph.build(
            num_pages=num_pages,
            page_roles=_page_roles_for_graph,
            visual_features=visual_features_list,
            semantic_features=semantic_features_list,
            link_graph=self.evidence_linker,
        )
        rg_summary = self.reasoning_graph.summary()
        edge_counts_str = ", ".join(
            "{}={}".format(k, v)
            for k, v in rg_summary['edge_type_counts'].items() if v
        )
        print(f"  Graph: {rg_summary['total_nodes']} nodes, "
              f"{rg_summary['total_edges']} edges ({edge_counts_str}), "
              f"avg importance={rg_summary['avg_graph_importance']:.3f}")

        # ============================================================
        # STAGE 4c: Visual Instruction Propagation Graph
        # Detects visual instructions ("see red text") and propagates
        # visual importance to pages whose features confirm the instruction.
        # ============================================================
        print("\nSTAGE 4c: Visual Instruction Graph...")
        self.visual_instruction_graph.build(texts, visual_features_list)
        vig_summary = self.visual_instruction_graph.summary()
        kw_str = ", ".join(
            "{}={}".format(k, v)
            for k, v in vig_summary['keyword_counts'].items() if v
        )
        print(f"  VIG: {vig_summary['total_instructions']} instructions, "
              f"{vig_summary['total_edges']} edges, "
              f"{vig_summary['pages_affected']} target pages"
              + (f" ({kw_str})" if kw_str else ""))

        # ============================================================
        # STAGE 5: Color Meaningfulness Gate (applied per-page later)
        # ============================================================

        # ============================================================
        # STAGE 6: Role-Aware Clustering (v13 — replaces TF-IDF KMeans)
        # ============================================================
        print("\nSTAGE 6: Role-Aware Clustering (v13)...")
        self.clusters, self.cluster_types = self._assign_role_aware_clusters(
            texts, visual_features_list, semantic_features_list
        )
        for cid, indices in self.clusters.items():
            ctype = self.cluster_types.get(cid, "unknown")
            print(f"  Cluster {cid} ({ctype}): {len(indices)} page(s)")
        if self._global_boosts_disabled:
            print("  Global context boosts: DISABLED (cluster collapse detected)")

        # ============================================================
        # STAGE 7: Context Propagation + Document-Level Rules
        # ============================================================
        print("\nSTAGE 7: Context Propagation...")
        self._apply_document_rules(num_pages)
        if self.global_boosts:
            print(f"  Global boosts: {self.global_boosts}")

        # ============================================================
        # STAGE 8+: Per-Page — Pertinence Engine + Decision
        # ============================================================
        print(f"\n{'='*60}")
        print("STAGE 8+: Pertinence Engine, Decision, Reasoning Trace")
        print(f"{'='*60}")

        results = []
        _page_ctx: List[Dict] = []   # per-page intermediate data for BERT refinement
        _doc_ctx = {
            'tfidf_scores':            tfidf_scores,
            'tfidf_top_terms_list':    tfidf_top_terms_list,
            'keyword_importance_list': keyword_importance_list,
        }
        for i in range(num_pages):
            result, page_ctx = self._process_single_page(i, _doc_ctx)
            results.append(result)
            _page_ctx.append(page_ctx)
            # Print per-page summary
            _pz = {"color": "COLOR", "bw": "B/W", "review_required": "REVIEW"}.get(result.decision_zone, "B/W")
            _ic = "✅" if result.should_use_color else ("🔶" if result.is_review_required else "❌")
            print(f"\nPage {i}: {_ic} {_pz}")
            _ctx = page_ctx
            print(f"  Role: {_ctx['page_role']}  |  Pertinence: {result.pertinence_score:.4f}  "
                  f"CaseAlign: {_ctx['case_alignment']:.2f}  CaseVisualMatch: {result.case_visual_match_score:.2f}")
            print(f"  Score: {result.final_score:.4f}  Confidence: {result.confidence:.2%}")
            print(f"  Visual: {result.visual_score:.4f}  Semantic: {result.semantic_score:.4f}  "
                  f"TF-IDF: {_ctx['tfidf_score']:.4f}  RefStrength: {_ctx['ref_strength']:.2f}")
            print(f"  Cluster: {result.cluster_id} ({result.cluster_type})  "
                  f"IncomingRefs: {_ctx['incoming_refs']}  ExhibitRes: {_ctx['is_exhibit_res']}")
            if result.dominant_signal != "none":
                print(f"  Dominant: {result.dominant_signal} ({_ctx.get('dominant_val', 0.0):.3f})")
            if result.conflict_type != "none":
                print(f"  Conflict: {result.conflict_type} (penalty -{_ctx['conflict_penalty']:.2f})")
            if _ctx['override_triggered']:
                _vs = "VALIDATED" if _ctx['validated_override'] else "REJECTED by context"
                print(f"  Override: {_ctx['override_reason']} [{_vs}]")

        # ──────────────────────────────────────────────────────────────
        # (per-page processing now handled by _process_single_page above)
        # ──────────────────────────────────────────────────────────────

        # ============================================================
        # BERT REFINEMENT PASS (v12 — conditional Legal-BERT)
        # ============================================================
        bert_candidates = [
            i for i, r in enumerate(results)
            if LegalBertEngine.should_activate(
                r.decision_zone,
                r.tfidf_similarity_score,
                r.conflict_type,
                r.semantic_score,
                # v14: pass visual_evidence_score (area-weighted, 0–1)
                # so BERT hard-blocks at ve < 0.15 and ve >= 0.5
                visual_features_list[i].visual_evidence_score,
            )
        ]

        if bert_candidates:
            print(f"\n{'='*60}")
            print(f"BERT Refinement: {len(bert_candidates)} page(s) flagged "
                  f"({', '.join(str(i) for i in bert_candidates)})")
            # Lazy-load BERT only now (avoids cost when no ambiguous pages)
            batch_texts = [texts[i] for i in bert_candidates]
            raw_bert    = self.legal_bert.get_legal_scores_batch(batch_texts)

            if self.legal_bert.is_available():
                for j, page_idx in enumerate(bert_candidates):
                    bert_score = float(raw_bert[j])
                    ctx    = _page_ctx[page_idx]
                    old_r  = results[page_idx]

                    # Fused semantic: 0.70 × BERT + 0.30 × TF-IDF
                    fused_semantic = self.legal_bert.fuse(
                        bert_score, old_r.tfidf_similarity_score
                    )

                    # Re-run pertinence with BERT-fused semantic component
                    new_pertinence = self.pertinence_engine.compute(
                        visual=ctx['visual'],
                        semantic=ctx['semantic'],
                        page_role=ctx['page_role'],
                        cluster_type=ctx['cluster_type'],
                        cluster_importance=ctx['cluster_importance'],
                        tfidf_score=ctx['tfidf_score'],
                        tfidf_top_terms=ctx['tfidf_top_terms'],
                        incoming_ref_count=ctx['incoming_refs'],
                        is_exhibit_resolution=ctx['is_exhibit_res'],
                        text=texts[page_idx],
                        override_triggered=ctx['override_triggered'],
                        override_reason=ctx['override_reason'],
                        reference_strength=ctx['ref_strength'],
                        bert_score=bert_score,                    # ← BERT drives semantic_combined
                        case_type=self._detected_case_type,       # v16
                        rolling_context_score=ctx.get('rolling_ctx_score', 0.0),   # DCE
                        prior_directive_count=ctx.get('prior_directive_count', 0),  # DCE
                        graph_importance=ctx.get('decision_importance', 0.0),       # strict decision graph (no chain inflation)
                        visual_propagation_score=ctx.get('visual_propagation_score', 0.0),  # VIG
                    )
                    new_pert_score  = new_pertinence.score
                    new_trace       = list(old_r.reasoning_trace) + [
                        f"BERT Refinement: bert_score={bert_score:.3f}, "
                        f"fused_semantic={fused_semantic:.3f}"
                    ] + new_pertinence.trace[-3:]  # append BERT-updated conclusion

                    # Dynamic override boost with BERT-adjusted scores
                    new_eff_pert = new_pert_score
                    if ctx['override_triggered'] and ctx['validated_override']:
                        ob = self._clamp(
                            0.10 + 0.20 * ctx['ref_strength'] + 0.10 * fused_semantic
                        )
                        new_eff_pert = min(0.95, new_pert_score + min(0.40, max(0.10, ob)))

                    # Re-check conflicts with updated semantic
                    new_conflict, new_penalty = self._detect_signal_conflicts(
                        ctx['visual_score'], fused_semantic,
                        new_eff_pert, ctx['incoming_refs'],
                    )

                    # Re-run arbitration with BERT-updated pertinence
                    new_arb = self.arbitration_engine.arbitrate(
                        dominant_factor=new_pertinence.dominant_factor,
                        visual_score=ctx['visual_score'],
                        semantic_score=fused_semantic,
                        effective_pertinence=new_eff_pert,
                        visual_propagation_score=ctx.get('visual_propagation_score', 0.0),
                        prior_directive_count=ctx.get('prior_directive_count', 0),
                        ref_strength=ctx['ref_strength'],
                        conflict_type=new_conflict,
                        page_role=ctx['page_role'],
                        case_type=self._detected_case_type,
                        visual_evidence_score=ctx['visual'].visual_evidence_score,
                    )
                    if new_arb.arbitrated_score != new_eff_pert:
                        new_trace.append(
                            f"Legal Arbitration (BERT pass) [{new_arb.priority_level}]: "
                            f"{new_eff_pert:.4f} → {new_arb.arbitrated_score:.4f}"
                            + (f" — {new_arb.conflict_resolution}" if new_arb.conflict_resolved else "")
                        )
                        new_eff_pert = new_arb.arbitrated_score

                    # Dominant signal with updated scores
                    new_dom_sig, new_dom_val = self._get_dominant_signal(
                        ctx['visual_score'], fused_semantic, new_eff_pert, ctx['ref_strength']
                    )
                    if new_dom_sig != "none":
                        new_base_score = self._clamp(
                            0.70 * new_dom_val + 0.30 * new_eff_pert
                        )
                    else:
                        new_base_score = self._clamp(
                            0.50 * new_eff_pert +
                            0.25 * ctx['visual_score'] +
                            0.25 * fused_semantic
                        )
                    new_final = self._apply_context_memory(page_idx, new_base_score)

                    new_confidence = self._calculate_confidence(
                        ctx['visual_score'], fused_semantic,
                        0.3, ctx['reference_score'], ctx['visual'], 0.8
                    )
                    new_confidence = self._clamp(new_confidence - new_penalty)
                    if ctx['page_role'] in ('evidence_photo', 'medical_image', 'exhibit_page'):
                        new_confidence = max(
                            new_confidence,
                            0.70 if new_conflict != "none" else 0.85
                        )

                    new_decision, new_zone = self._make_decision_v10(
                        new_eff_pert, new_confidence, new_conflict
                    )
                    new_color    = (new_decision == Decision.COLOR)
                    new_review   = (new_decision == Decision.REVIEW_REQUIRED)

                    # ── v22: Conflict gate (BERT pass) ─────────────────────
                    # Mirror the main-pass conflict gate so BERT-inflated semantic
                    # scores cannot silently re-inflate B/W pages to REVIEW.
                    # Same conflict conditions as the primary decision path.
                    _bert_high_auth = ctx['page_role'] in (
                        'evidence_photo', 'medical_image', 'exhibit_page',
                        'financial_chart', 'signature_page'
                    )
                    _bert_conflict_A = fused_semantic >= 0.35 and ctx['visual_score'] >= 0.30
                    _bert_conflict_B = ctx['ref_strength'] >= 0.45 and fused_semantic < 0.20
                    if (new_review
                            and not _bert_high_auth
                            and not (_bert_conflict_A or _bert_conflict_B)):
                        new_decision = Decision.BW
                        new_zone     = "bw"
                        new_color    = False
                        new_review   = False
                        new_trace.append(
                            f"BERT REVIEW→B/W (v22): no signal conflict — "
                            f"fused_sem={fused_semantic:.3f}, visual={ctx['visual_score']:.3f}, "
                            f"ref={ctx['ref_strength']:.3f}"
                        )

                    # v22: Highlighted-text → hard COLOR forcing (BERT pass)
                    # Same thresholds and semantic gate as primary pass.
                    _bert_vf        = visual_features_list[page_idx]
                    _bert_hl_yellow = getattr(_bert_vf, 'yellow_highlighted_text_density', 0.0)
                    _bert_hl_any    = _bert_vf.highlighted_text_density
                    _bert_hl_area   = (_bert_hl_yellow >= 0.001) or (_bert_hl_any >= 0.0015)
                    _bert_tfidf     = _page_ctx[page_idx].get('tfidf_score', 0.0)
                    _bert_sem       = ctx.get('semantic_score', fused_semantic)
                    _bert_hl_sem    = (_bert_tfidf >= 0.05) or (_bert_sem >= 0.20)
                    if (not new_color
                            and not _page_ctx[page_idx].get('hint_force_review')
                            and _bert_hl_area and _bert_hl_sem):
                        new_decision = Decision.COLOR
                        new_zone     = "color"
                        new_color    = True
                        new_review   = False
                        _b_hl_label  = "yellow" if _bert_hl_yellow >= 0.001 else "highlight"
                        _b_hl_val    = _bert_hl_yellow if _bert_hl_yellow >= 0.001 else _bert_hl_any
                        new_trace.append(
                            f"BERT HIGHLIGHT-TEXT-COLOR (v22): "
                            f"{_b_hl_label}_text_density={_b_hl_val:.4f} "
                            f"+ tfidf={_bert_tfidf:.3f}/sem={_bert_sem:.3f} — "
                            "attorney-annotated relevant text preserved as COLOR"
                        )

                    # v22: Directive visual-feature confirmation (BERT pass)
                    _bert_link = self.evidence_linker.get_link_info(page_idx)
                    if (not new_color
                            and not _page_ctx[page_idx].get('hint_force_review')
                            and _bert_link.directive_visual_confirmed):
                        new_decision = Decision.COLOR
                        new_zone     = "color"
                        new_color    = True
                        new_review   = False
                        _feat_list   = ", ".join(_bert_link.directive_visual_features)
                        new_trace.append(
                            f"BERT DIRECTIVE-COLOR (v22): directive from previous page "
                            f"+ visual feature(s) confirmed [{_feat_list}] → COLOR"
                        )

                    # v22: Directive self-confirmation (BERT pass)
                    # Source page has directive AND its own visual feature confirmed.
                    # Guard: require ve >= 0.10 to prevent text-only self-confirmation.
                    _bert_self_ve_ok = visual_features_list[page_idx].visual_evidence_score >= 0.10
                    if (not new_color
                            and not _page_ctx[page_idx].get('hint_force_review')
                            and _bert_link.directive_self_confirmed
                            and _bert_self_ve_ok):
                        new_decision = Decision.COLOR
                        new_zone     = "color"
                        new_color    = True
                        new_review   = False
                        _sf_list = ", ".join(_bert_link.directive_self_features)
                        new_trace.append(
                            f"BERT DIRECTIVE-SELF-COLOR (v22): page directs to [{_sf_list}] "
                            "confirmed on same page → COLOR"
                        )

                    # v22: Semantic continuity COLOR path (BERT pass)
                    # Target page is cited by a directive page AND both pages have
                    # meaningful semantic content (shared topic thread) → COLOR.
                    # Covers pure-text directives: "see details below" with no visual
                    # but continuous legal argument across both pages.
                    if (not new_color
                            and not _page_ctx[page_idx].get('hint_force_review')
                            and _bert_link.directive_count == 0   # this IS the target page
                            and _bert_link.referenced_by):
                        _citing_idx  = _bert_link.referenced_by[0]
                        _citing_ctx  = _page_ctx[_citing_idx] if 0 <= _citing_idx < len(_page_ctx) else {}
                        _citing_dir  = _citing_ctx.get('prior_directive_count', 0)
                        _citing_tfidf = _citing_ctx.get('tfidf_score', 0.0)
                        _this_tfidf  = _page_ctx[page_idx].get('tfidf_score', 0.0)
                        _citing_sem  = _citing_ctx.get('semantic_score',
                                           _citing_ctx.get('visual_score', 0.0))
                        # Semantic continuity: citing page has a directive AND both
                        # pages have relevant TF-IDF or BERT scores
                        _sem_continuity = (
                            _citing_dir > 0
                            and _citing_tfidf >= 0.05
                            and _this_tfidf   >= 0.05
                        )
                        # Visual + semantic: visual confirmed on either page AND
                        # semantic continuity
                        _vis_sem_continuity = (
                            _citing_dir > 0
                            and fused_semantic >= 0.35
                            and (_citing_tfidf >= 0.05 or _this_tfidf >= 0.05)
                        )
                        if _sem_continuity or _vis_sem_continuity:
                            new_decision = Decision.COLOR
                            new_zone     = "color"
                            new_color    = True
                            new_review   = False
                            _path = "semantic" if _sem_continuity else "visual+semantic"
                            new_trace.append(
                                f"BERT CONTINUITY-COLOR (v22): page {_citing_idx+1} "
                                f"directive + {_path} continuity "
                                f"(citing_tfidf={_citing_tfidf:.3f}, "
                                f"this_tfidf={_this_tfidf:.3f}, "
                                f"fused_sem={fused_semantic:.3f}) → COLOR"
                            )

                    # v23: MEANINGFUL-COLOR gate (BERT pass)
                    # Mirrors primary pass — visual analyzer confirmed meaningful body
                    # color, trust it directly.
                    _bert_vf2 = visual_features_list[page_idx]
                    if (not new_color
                            and not _page_ctx[page_idx].get('hint_force_review')
                            and _bert_vf2.is_color_meaningful
                            and _bert_vf2.color_density >= 0.015):
                        new_decision = Decision.COLOR
                        new_zone     = "color"
                        new_color    = True
                        new_review   = False
                        new_trace.append(
                            f"BERT MEANINGFUL-COLOR (v23): visual analyzer confirmed "
                            f"meaningful body color (density={_bert_vf2.color_density:.3f}) → COLOR"
                        )

                    # Persist user hint "review" zone override through BERT pass
                    if _page_ctx[page_idx].get('hint_force_review'):
                        new_decision = Decision.REVIEW_REQUIRED
                        new_zone     = "review_required"
                        new_color    = False
                        new_review   = True

                    # BERT influences decisions through fused_semantic fed into
                    # pertinence_engine.compute() above — it does NOT make binary
                    # final decision flips here.  The re-scored new_decision from
                    # _make_decision_v10(new_eff_pert, new_confidence, new_conflict)
                    # is the authoritative result.  Keeping BERT as a score influence
                    # (not a final override) preserves the full scoring pipeline and
                    # prevents BERT from single-handedly reversing a decision that the
                    # visual, semantic, and pertinence engines agreed on.
                    new_trace.append(
                        f"BERT influence applied via fused_semantic={fused_semantic:.3f} "
                        f"(bert={bert_score:.3f}) — final decision from scoring pipeline"
                    )

                    new_dftext = self._get_dominant_factor_text(
                        new_pertinence.dominant_factor,
                        ctx['visual'], ctx['semantic'], ctx['page_role'],
                        ctx['incoming_refs'], new_eff_pert, new_color, new_review,
                        link_info=self.evidence_linker.get_link_info(page_idx),
                        prior_directive_count=ctx.get('prior_directive_count', 0),
                        gate_forced_color=old_r.gate_forced_color,
                        reasoning_trace=new_trace,
                    )

                    # Build updated result (copy fields, overwrite changed ones)
                    updated = DecisionResult(
                        page_id=page_idx,
                        should_use_color=new_color,
                        confidence=new_confidence,
                        final_score=new_final,
                        explanation={
                            **old_r.explanation,
                            'semantic': {
                                **old_r.explanation.get('semantic', {}),
                                'bert_score':     bert_score,
                                'fused_semantic': fused_semantic,
                                'bert_activated': True,
                            },
                        },
                        is_override=old_r.is_override,
                        override_reason=old_r.override_reason,
                        page_role=old_r.page_role,
                        cluster_id=old_r.cluster_id,
                        cluster_type=old_r.cluster_type,
                        tfidf_similarity_score=old_r.tfidf_similarity_score,
                        visual_score=ctx['visual_score'],
                        semantic_score=fused_semantic,
                        reasoning=old_r.reasoning,
                        pertinence_score=new_pert_score,
                        reasoning_trace=new_trace,
                        dominant_signal=new_dom_sig,
                        conflict_type=new_conflict,
                        decision_zone=new_zone,
                        is_review_required=new_review,
                        dominant_factor_text=new_dftext,
                        bert_score=bert_score,
                        bert_activated=True,
                        # v16/v17 fields preserved through refinement
                        case_type=old_r.case_type,
                        important_visual_types=old_r.important_visual_types,
                        cross_page_reference=old_r.cross_page_reference,
                        case_visual_match_score=old_r.case_visual_match_score,
                        propagation_boost=0.0,   # v18: set by _propagate_evidence_scores()
                        # v19: legal arbitration (updated by BERT pass)
                        authority_weight=new_arb.authority_weight,
                        priority_level=new_arb.priority_level,
                        arbitration_justification=new_arb.justification,
                        conflict_resolved=new_arb.conflict_resolved,
                        # v23: preserve gate protection through BERT pass
                        gate_forced_color=old_r.gate_forced_color,
                    )
                    results[page_idx] = updated

                    zone_lbl = {"color": "COLOR", "bw": "B/W", "review_required": "REVIEW"}.get(new_zone, "B/W")
                    changed  = "→ changed" if new_zone != old_r.decision_zone else "(unchanged)"
                    print(f"  Page {page_idx}: BERT={bert_score:.3f}  "
                          f"fused_sem={fused_semantic:.3f}  "
                          f"pert={new_pert_score:.4f}  {zone_lbl} {changed}")
            else:
                print("  LegalBERT not available — BERT refinement skipped")

        # ============================================================
        # FINAL DECISION LAW (v14 — visual truth trumps all)
        # ============================================================
        # After all refinements, apply the absolute visual evidence gates.
        # These cannot be overridden by pertinence scoring or BERT.
        #
        #   ve_score > 0.5  → COLOR   (strong visual evidence confirmed)
        #   ve_score < 0.15 → B/W     (no visual truth; decorative/layout)
        #   else            → keep decision from pertinence + BERT
        #
        # Reference boost floor: pages cited elsewhere get a minimum ve floor of 0.25
        # so cross-referenced text pages are not silently killed by B/W gate.
        # Validated hard overrides (stamps, signatures) bypass the B/W gate.
        # ============================================================
        print(f"\n{'='*60}")
        print("FINAL DECISION LAW (v14): visual truth gates")
        for i, r in enumerate(results):
            ve = visual_features_list[i].visual_evidence_score
            link_info_i = self.evidence_linker.get_link_info(i)

            # Reference boost floor
            if link_info_i.incoming_ref_count > 0:
                ve = max(ve, 0.25)

            if ve > 0.5 and not r.should_use_color:
                results[i].should_use_color = True
                results[i].decision_zone    = "color"
                results[i].is_review_required = False
                results[i].reasoning_trace.append(
                    f"FINAL LAW (v14): visual_evidence_score={ve:.3f} > 0.50 → PROMOTED to COLOR"
                )
                print(f"  Page {i}: ve={ve:.3f} → PROMOTED COLOR")

            elif ve < 0.15 and r.should_use_color and not r.is_override:
                # Only demote if the decision wasn't from a validated hard override
                # AND wasn't forced by a hard visual gate (VIG/highlight/directive/meaningful).
                # Gate-forced COLOR pages have confirmed visual evidence — demoting them
                # would throw away the specific signal the gate was designed to protect.
                if r.gate_forced_color:
                    results[i].reasoning_trace.append(
                        f"FINAL LAW (v14): ve={ve:.3f} < 0.15 but gate_forced_color=True "
                        f"— hard gate anchors this decision, demotion suppressed"
                    )
                else:
                    results[i].should_use_color = False
                    results[i].decision_zone    = "bw"
                    results[i].is_review_required = False
                    results[i].reasoning_trace.append(
                        f"FINAL LAW (v14): visual_evidence_score={ve:.3f} < 0.15 → DEMOTED to B/W"
                    )
                    print(f"  Page {i}: ve={ve:.3f} → DEMOTED B/W")

        # ============================================================
        # SCORE PROPAGATION PASS (v18 — evidence chaining)
        # ============================================================
        # A real paralegal connects evidence across pages:
        #   "Page 7 is Exhibit A. Pages 3, 8, and 12 reference it in key
        #    testimony context → Page 7 should be COLOR regardless of how
        #    sparse its own visual signal is."
        #
        # Propagation: for each page that is cited by other pages,
        # compute a boost from the final scores of the citing pages,
        # weighted by reference intent (exhibit_ref=1.0 > page_ref=0.7).
        # This boost nudges scores but cannot override the visual truth law.
        # ============================================================
        results = self._propagate_evidence_scores(results)

        # ============================================================
        # STAGE: Document-level confidence normalization (v19)
        # ============================================================
        # After all refinements, recalibrate REVIEW_REQUIRED pages using
        # z-scores relative to the document-wide score distribution.
        # A page at score 0.45 in a COLOR-heavy doc (mean 0.65) is below
        # average; a page at 0.45 in a B/W doc (mean 0.12) is a clear COLOR.
        print(f"\n{'='*60}")
        print("STAGE: Document Normalization (v19)")
        self.arbitration_engine.normalize_document_confidence(results)
        review_pages = [r.page_id for r in results if r.is_review_required]
        if review_pages:
            print(f"  Normalized {len(review_pages)} REVIEW page(s): {review_pages}")
        else:
            print("  No REVIEW pages to normalize")

        print(f"\n{'='*60}")
        print(f"CPCE v19 Complete: {len(results)} pages processed")
        print(f"{'='*60}\n")

        return results

    # ============================================================
    # STAGE 8 WORKER: Per-page pertinence + arbitration + decision
    # ============================================================
    def _process_single_page(
        self,
        page_idx: int,
        ctx: Dict[str, Any],
    ) -> Tuple["DecisionResult", Dict[str, Any]]:
        """
        Process one page through the full pipeline:
          role classification → visual/semantic scoring → pertinence engine
          → conflict detection → legal arbitration → 3-zone decision

        Returns (DecisionResult, page_ctx_entry) where page_ctx_entry is the
        intermediate data consumed by the BERT refinement pass.
        """
        img, text, visual, semantic = self._pages_data[page_idx]
        tfidf_score     = ctx['tfidf_scores'][page_idx]
        tfidf_top_terms = ctx['tfidf_top_terms_list'][page_idx]
        kw_importance   = ctx['keyword_importance_list'][page_idx]

        # Page role (v7)
        page_role = self._classify_page_role_v7(text, visual, semantic)

        # Signature pages in contract disputes are case-critical evidence
        # (they prove execution or contest non-execution of the contract).
        # With no color ink detected, visual_score=0 even when case_visual_match=0.75
        # because the multiplier operates on a zero base.  Apply a minimum
        # visual_evidence_score so the pertinence engine has signal to work with.
        if page_role == 'signature_page' and self._detected_case_type == 'contract_dispute':
            visual.visual_evidence_score = max(visual.visual_evidence_score, 0.40)
            visual.is_color_meaningful = True

        # Correspondence pages (emails, letters) contain social media icon bars
        # in footers/signatures that reliably pass ChartValidator due to their
        # colorful, evenly-spaced structure.  Strip chart visual features NOW,
        # before the Chart Dominance Rule below can use them to flip the role
        # to 'financial_chart' or boost visual_evidence_score.
        if page_role == 'correspondence':
            visual.chart_regions = 0
            visual.chart_region_data = []
            # Email borders and social media icon color bleed produce
            # stamp_density readings of 0.005-0.015 — far below a real
            # notary seal (typically > 0.020).  Suppress low readings so
            # they don't inflate the visual_score via stamp_score.
            if visual.stamp_density < 0.020:
                visual.stamp_density = 0.0
            # With charts zeroed and stamps suppressed, visual_evidence_score
            # reflects only real photo content.  If there are no photos
            # either, null out the score so the page doesn't register as
            # visually meaningful (was previously 0.25+ from false charts).
            if visual.photo_regions == 0:
                visual.visual_evidence_score = 0.0
                visual.is_color_meaningful = False

        # v15: Chart Dominance Rule — runs BEFORE the visual gate so the boosted
        # ve_score is read when the gate checks thresholds.
        # 3+ ChartValidator-confirmed charts = this page IS financial/evidence content,
        # regardless of how small each chart region is.
        if visual.chart_regions >= 3:
            visual.visual_evidence_score = max(visual.visual_evidence_score, 0.55)
            if page_role in ('decorative_or_layout', 'legal_argument',
                             'boilerplate_text', 'unknown'):
                page_role = 'financial_chart'

        # v13: Role-visual gate — NEVER assign evidence roles without visual confirmation
        ve_score = visual.visual_evidence_score

        # v18: Photo-confirmed preservation — if the visual classifier detected actual photo
        # regions, the role is real regardless of ve_score.  A small photo on a page has a
        # small area ratio, so ve_score is low — but the photo itself is the evidence.
        # Demoting to decorative_or_layout here causes the downstream override validator to
        # reject a legitimate HIGH_RISK evidence_photo decision ("REJECTED by context").
        #
        # v19: Extended to grayscale images (B&W photos) and signatures — these also have
        # ve_score ≈ 0 because the color-based classifier never runs on them.
        _has_grayscale = getattr(visual, 'grayscale_regions', 0) > 0
        _has_sig       = getattr(visual, 'signature_regions', 0) > 0
        _has_bw_stamp  = getattr(visual, 'bw_stamp_regions',  0) > 0

        # v20: Stamp keyword context boost — a B&W stamp near official terminology
        # (notary, seal, certified) is unambiguous legal evidence.  Boost ve_score so
        # the page reaches the color decision without needing a high area-ratio score.
        if _has_bw_stamp:
            _stamp_kws = [
                'seal', 'notary', 'certified', 'certification', 'sworn',
                'acknowledged', 'commissioner', 'official', 'attest',
                'county of', 'state of', 'apostille', 'affixed',
            ]
            _text_lower = text.lower()
            if any(kw in _text_lower for kw in _stamp_kws):
                visual.visual_evidence_score = max(visual.visual_evidence_score, 0.45)
                visual.is_color_meaningful = True

        if page_role in ('evidence_photo', 'medical_image') and (
            visual.photo_regions > 0 or _has_grayscale
        ):
            visual.visual_evidence_score = max(visual.visual_evidence_score, 0.30)
            ve_score = visual.visual_evidence_score
        elif page_role == 'signature_page' and (_has_sig or _has_bw_stamp):
            # Shape-confirmed signature/seal → floor ve_score so the page is never
            # demoted back to decorative layout by the ve_score gate below.
            visual.visual_evidence_score = max(visual.visual_evidence_score, 0.25)
            ve_score = visual.visual_evidence_score
        elif page_role in ('evidence_photo', 'medical_image', 'financial_chart', 'exhibit_page'):
            if ve_score < 0.3:
                page_role = 'decorative_or_layout'
        # Allow re-promotion if visual evidence is confirmed but role was downgraded earlier
        elif page_role == 'decorative_or_layout' or page_role == 'unknown':
            if visual.chart_regions > 3 and ve_score >= 0.3:
                page_role = 'financial_chart'
            elif visual.photo_regions > 0 and ve_score >= 0.3:
                page_role = 'evidence_photo'

        # Cluster info
        cluster_id = self._get_cluster_id(page_idx)
        cluster_type = self.cluster_types.get(cluster_id, "unknown") if cluster_id is not None else "unknown"
        cluster_importance = self._get_cluster_importance(cluster_id)

        # Evidence link info (v8)
        link_info = self.evidence_linker.get_link_info(page_idx)
        incoming_refs = link_info.incoming_ref_count
        is_exhibit_res = link_info.is_exhibit_resolution

        # DCE: rolling context from prior pages
        # prior_directive_count = how many "as shown below / see following" directives
        # the PREVIOUS page had — implies this page is the referenced content.
        prev_link = self.evidence_linker.get_link_info(page_idx - 1) if page_idx > 0 else None
        prior_directive_count = prev_link.directive_count if prev_link else 0
        rolling_ctx_score = self.evidence_linker.get_rolling_context_score(page_idx)

        # Reasoning graph:
        #   graph_importance    — full propagation, used for explanation text only
        #   decision_importance — strict propagation (exhibit_ref+visual_match),
        #                         used for pertinence scoring to prevent chain inflation
        graph_importance    = self.reasoning_graph.get_graph_importance(page_idx)
        decision_importance = self.reasoning_graph.get_decision_importance(page_idx)
        reasoning_chain     = self.reasoning_graph.get_reasoning_chain(page_idx)

        # Visual Instruction Graph — cross-page visual meaning propagation
        visual_propagation_score   = self.visual_instruction_graph.get_visual_propagation_score(page_idx)
        visual_instruction_context = self.visual_instruction_graph.get_visual_instruction_context(page_idx)
        # Merge VIG context into the reasoning chain for explanation output
        if visual_instruction_context:
            reasoning_chain = list(reasoning_chain) + visual_instruction_context

        # Reference score for confidence formula
        reference_score = 0.2 if semantic.exhibit_mentions > 0 else 0.0
        if 'reference' in self.global_boosts:
            reference_score = min(1.0, reference_score + self.global_boosts['reference'])
        if incoming_refs > 0:
            reference_score = min(1.0, reference_score + incoming_refs * 0.10)

        # v17: case visual match — must be computed before override and visual gate
        case_visual_match = self._compute_case_visual_match(
            visual, text, page_role, self._detected_case_type
        )

        # Raw override check — v17: passes actual page_role and case_type
        override_triggered, override_reason = self._check_override(
            visual, semantic, PageRole.UNKNOWN,
            case_type=self._detected_case_type,
            page_role_str=page_role,
        )

        # ── v10/v17: Color meaningfulness gate — case-aware
        is_meaningful = self._is_color_meaningful(
            visual, PageRole.UNKNOWN, case_type=self._detected_case_type
        )
        visual_priorities = self.case_type_detector.get_color_priority_for_case(
            self._detected_case_type
        )
        visual_score = self._calculate_visual_score_v7(visual, visual_priorities)
        # Soft penalty when no meaningful color AND no high-value visual elements
        if not is_meaningful and visual.photo_regions == 0 and visual.chart_regions == 0:
            visual_score *= 0.25

        # Blend v10 visual_evidence_score: region-level classification refines visual_score
        if visual.visual_evidence_score > 0:
            visual_score = self._clamp(
                visual_score * 0.65 + visual.visual_evidence_score * 0.35
            )

        # v17: Case visual match gate — discount visuals that don't match this case type.
        # A contract doc where every page has "charts" (table borders) must not color
        # those pages just because visuals were detected.
        # Only applies when no raw override was triggered (overrides bypass this gate).
        _case_visual_discount_applied = False
        if not override_triggered and case_visual_match < 0.3:
            # v18: Never deep-discount confirmed visual elements in evidence clusters.
            # Clustering established these pages are near key evidence — case_visual_match
            # penalizes missing financial keywords on a specific page, but the cluster
            # membership already attests to evidential relevance.
            _in_evidence_cluster = cluster_type == 'evidence_cluster'
            _has_confirmed_visuals = visual.chart_regions > 0 or visual.photo_regions > 0
            if _in_evidence_cluster and _has_confirmed_visuals:
                pass  # Trust cluster context; skip discount for confirmed visual elements
            else:
                discount = max(0.25, case_visual_match / 0.3)   # [0.25, 1.0]
                visual_score = self._clamp(visual_score * discount)
                _case_visual_discount_applied = True

        # ── v9: Case context alignment boosts semantic score ─────────
        case_alignment = self._compute_case_alignment_score(text)
        # v17: pass case_type to use case-specific keywords for semantic scoring
        semantic_score = self._calculate_semantic_score_v7(
            semantic, tfidf_score, kw_importance, text,
            case_type=self._detected_case_type,
        )
        # Blend case alignment: pages aligned with case type get a soft semantic boost
        semantic_score = self._clamp(semantic_score * (1.0 + 0.15 * case_alignment))

        # Reference intent strength from evidence link graph
        ref_strength = link_info.reference_strength

        # ── v10 CORE: Pertinence Engine (spec formula) ─────────
        pertinence_result = self.pertinence_engine.compute(
            visual=visual,
            semantic=semantic,
            page_role=page_role,
            cluster_type=cluster_type,
            cluster_importance=cluster_importance,
            tfidf_score=tfidf_score,
            tfidf_top_terms=tfidf_top_terms,
            incoming_ref_count=incoming_refs,
            is_exhibit_resolution=is_exhibit_res,
            text=text,
            override_triggered=override_triggered,
            override_reason=override_reason,
            reference_strength=ref_strength,        # v10: intent-classified strength
            case_type=self._detected_case_type,     # v16: case-aware scoring
            rolling_context_score=rolling_ctx_score,    # DCE: narrative continuity
            prior_directive_count=prior_directive_count, # DCE: forward directives
            graph_importance=decision_importance,        # strict decision graph (no chain inflation)
            visual_propagation_score=visual_propagation_score,  # VIG: cross-page visual instruction
        )

        pertinence_score = pertinence_result.score
        reasoning_trace = list(pertinence_result.trace)   # copy so we can extend

        # v17: annotate trace with case visual match result
        if _case_visual_discount_applied:
            reasoning_trace.append(
                f"Case Visual Match Gate: case_visual_match={case_visual_match:.3f} < 0.30 — "
                f"visual_score discounted (case='{self._detected_case_type}', "
                f"visuals don't match what matters for this case type)"
            )
        validated_override = pertinence_result.override_valid

        # v10: append visual element classification reasoning
        if visual.region_classifications:
            reasoning_trace.append(
                f"Visual Elements ({len(visual.region_classifications)} region(s), "
                f"evidence_score={visual.visual_evidence_score:.3f}):"
            )
            for rc in visual.region_classifications:
                reasoning_trace.append(
                    f"  {rc.element_type.upper()} conf={rc.confidence:.2f} "
                    f"impact={rc.pertinence_impact:.2f} bbox={rc.bbox}"
                )

        # ── v9: Dynamic override boost (replaces static +0.30) ───────
        # boost = f(reference_strength, semantic_alignment) so strong evidence → larger boost
        effective_pertinence = pertinence_score
        if override_triggered and validated_override:
            override_boost = self._clamp(0.10 + 0.20 * ref_strength + 0.10 * semantic_score)
            override_boost = min(0.40, max(0.10, override_boost))
            effective_pertinence = min(0.95, pertinence_score + override_boost)
            reasoning_trace.append(
                f"Dynamic override boost: +{override_boost:.3f} "
                f"(ref_strength={ref_strength:.2f}, semantic={semantic_score:.2f})"
            )

        # ── v20: Visual Truth Override ────────────────────────────────
        # Evidence photos are SELF-EVIDENTIARY: the photo IS the legal evidence.
        # A paralegal does not require textual corroboration to confirm that a
        # photograph of an accident scene, injury, or document must print in color.
        # When the page role and visual classifier both confirm a real photo with
        # strong visual score, floor effective_pertinence above the COLOR threshold
        # so the text-signal deficit doesn't veto a clearly visual evidence page.
        _vt_photo_confirmed = (
            page_role in ('evidence_photo', 'medical_image')
            and visual_score >= 0.70
            and (visual.photo_regions > 0 or getattr(visual, 'grayscale_regions', 0) > 0)
        )
        if _vt_photo_confirmed and effective_pertinence < 0.76:
            reasoning_trace.append(
                f"Visual Truth Override: confirmed photo page (role='{page_role}', "
                f"visual={visual_score:.3f} ≥ 0.70) — pertinence floored at 0.76 "
                f"(was {effective_pertinence:.3f}) — photos are self-evidentiary"
            )
            effective_pertinence = 0.76

        # ── v9: Contradiction detection ───────────────────────────────
        conflict_type, conflict_penalty = self._detect_signal_conflicts(
            visual_score, semantic_score, effective_pertinence, incoming_refs,
            page_role=page_role,
        )
        if conflict_type != "none":
            reasoning_trace.append(
                f"Signal conflict: '{conflict_type}' — "
                f"confidence reduced by {conflict_penalty:.2f}"
            )

        # ── Legal Arbitration ─────────────────────────────────────────
        # Applies legal evidence hierarchy and directed conflict resolution.
        # Maps dominant_factor to authority weight; self-evidently probative
        # signals (photos, stamps) command COLOR without semantic corroboration.
        # Conflict resolution decides WHICH signal to trust, not just penalizes.
        arb_result = self.arbitration_engine.arbitrate(
            dominant_factor=pertinence_result.dominant_factor,
            visual_score=visual_score,
            semantic_score=semantic_score,
            effective_pertinence=effective_pertinence,
            visual_propagation_score=visual_propagation_score,
            prior_directive_count=prior_directive_count,
            ref_strength=ref_strength,
            conflict_type=conflict_type,
            page_role=page_role,
            case_type=self._detected_case_type,
            visual_evidence_score=visual.visual_evidence_score,
        )
        if arb_result.arbitrated_score != effective_pertinence:
            reasoning_trace.append(
                f"Legal Arbitration [{arb_result.priority_level}] "
                f"(authority={arb_result.authority_weight:.2f}): "
                f"score {effective_pertinence:.4f} → {arb_result.arbitrated_score:.4f}"
                + (f" — {arb_result.conflict_resolution}" if arb_result.conflict_resolved else "")
            )
            effective_pertinence = arb_result.arbitrated_score

        # ── v9: Dominant signal logic ─────────────────────────────────
        dominant_signal, dominant_val = self._get_dominant_signal(
            visual_score, semantic_score, effective_pertinence, ref_strength
        )
        if dominant_signal != "none":
            # Dominant signal: 70% weight on the dominant channel, 30% on pertinence
            base_final_score = self._clamp(
                0.70 * dominant_val + 0.30 * effective_pertinence
            )
            reasoning_trace.append(
                f"Dominant signal: '{dominant_signal}' ({dominant_val:.3f}) — "
                f"drives decision (70% weight)"
            )
        else:
            base_final_score = self._clamp(
                0.50 * effective_pertinence +
                0.25 * visual_score +
                0.25 * semantic_score
            )

        final_score = self._apply_context_memory(page_idx, base_final_score)
        confidence = self._calculate_confidence(
            visual_score, semantic_score, 0.3, reference_score, visual, 0.8
        )
        # Apply conflict penalty to confidence
        confidence = self._clamp(confidence - conflict_penalty)
        if page_role in ('evidence_photo', 'medical_image', 'exhibit_page'):
            # Guarantee minimum confidence for high-value pages; lower floor if conflicted
            confidence = max(confidence, 0.70 if conflict_type != "none" else 0.85)

        # ── User Hint ─────────────────────────────────────────────
        # Applied after arbitration, before final zone decision.
        # Paralegal/attorney hints can floor (pertinent), cap (decorative),
        # or force review (review) — with safety guards on both directions.
        hint_result = self.user_hint_engine.apply(
            page_idx=page_idx,
            effective_pertinence=effective_pertinence,
            visual_evidence_score=visual.visual_evidence_score,
            authority_weight=arb_result.authority_weight,
        )
        if hint_result.hint_applied:
            effective_pertinence = hint_result.effective_pertinence
            reasoning_trace.append(hint_result.override_reason)
        if hint_result.warning:
            reasoning_trace.append(f"HINT WARNING: {hint_result.warning}")
        _hint_force_review = hint_result.force_review

        # ── v10: 3-zone pertinence decision ──────────────────────
        decision, decision_zone = self._make_decision_v10(
            effective_pertinence, confidence, conflict_type
        )
        should_use_color = (decision == Decision.COLOR)
        is_review_required = (decision == Decision.REVIEW_REQUIRED)

        # ── v22: Conflict-based REVIEW gate ──────────────────────
        # Philosophy: REVIEW = ambiguity from conflicting signals, not signal strength.
        # A single strong signal (high semantic alone, or refs alone) is NOT ambiguous —
        # it is a confident B/W indicator.  Ambiguity arises only when two evidence
        # types pull in different directions simultaneously.
        #
        # Conflict condition A — evidence type contradiction:
        #   semantic ≥ 0.35 AND visual ≥ 0.30
        #   Both textual and visual evidence are substantial → genuine ambiguity about
        #   whether the color element is the carrier of evidence or the text is.
        #
        # Conflict condition B — structural vs content mismatch:
        #   ref_strength ≥ 0.45 AND semantic < 0.20
        #   A strong exhibit reference exists but the page itself has no content —
        #   possible OCR failure or blank evidence page; paralegal must verify.
        #
        # Exempt: high-authority role pages (photos, medical, exhibits) are
        # always worth a paralegal's eyes regardless of signal pattern.
        # ── v22: Zero-color hard B/W gate ─────────────────────────
        # color_density == 0.0 means not a single colored pixel exists on the page.
        # Nothing can be lost by printing in B/W — force it immediately.
        # Also: signature_page with effectively no color (density < 0.01) AND no
        # stamp/highlight has no colorimetric evidence worth preserving.
        _sig_no_color = (
            page_role == 'signature_page'
            and visual.color_density < 0.01
            and visual.stamp_density < 0.0005
            and visual.highlight_density < 0.001
        )
        if visual.color_density == 0.0 or _sig_no_color:
            decision           = Decision.BW
            decision_zone      = "bw"
            should_use_color   = False
            is_review_required = False
            _reason = (
                "zero color pixels on page — nothing to lose in B/W"
                if visual.color_density == 0.0
                else f"signature page with color_density={visual.color_density:.3f} "
                     "and no stamp/highlight — print B/W"
            )
            reasoning_trace.append(f"ZERO-COLOR→B/W (v22): {_reason}")

        _high_auth_role = page_role in ('evidence_photo', 'medical_image', 'exhibit_page',
                                        'financial_chart', 'signature_page')
        _conflict_A = semantic_score >= 0.35 and visual_score >= 0.30
        _conflict_B = ref_strength   >= 0.45 and semantic_score < 0.20
        _review_conflict = _conflict_A or _conflict_B
        if (decision == Decision.REVIEW_REQUIRED
                and not _high_auth_role
                and not _review_conflict):
            decision       = Decision.BW
            decision_zone  = "bw"
            should_use_color   = False
            is_review_required = False
            reasoning_trace.append(
                f"REVIEW→B/W (v22): no signal conflict — "
                f"semantic={semantic_score:.3f}, visual={visual_score:.3f}, "
                f"ref={ref_strength:.3f} — strong single signal is confident B/W, not ambiguous"
            )

        # Tracks whether a hard gate (VIG / highlight / directive / meaningful) has
        # forced COLOR on this page — used by WEAK-EVIDENCE-DEMOTE below to avoid
        # cancelling legitimate gate-driven decisions.
        _gate_forced_color: bool = False

        # ── v22: VIG-COLOR forcing ─────────────────────────────────
        # A visual instruction on another page explicitly targets a feature that
        # exists on this page (propagation ≥ 0.30) AND some visual signal is present.
        # The VIG already verified the feature match (highlight_density, photo_regions,
        # etc.) — no additional semantic gate needed.  Visual instructions are
        # self-confirming when the target feature exists.
        # Feature-based check: the VIG already confirmed the feature on the target page,
        # so the derived visual_score (which ignores highlight_density) is not the right gate.
        # Accept any of: strong visual signal, confirmed highlight, or meaningful color.
        _vig_feature_ok = (
            visual_score > 0.05
            or getattr(visual, 'highlight_density', 0.0) > 0.001
            or (visual.color_density > 0.010 and visual.is_color_meaningful)
        )
        if (not should_use_color
                and not is_review_required
                and not _hint_force_review
                and visual_propagation_score >= 0.30
                and _vig_feature_ok):
            decision       = Decision.COLOR
            decision_zone  = "color"
            should_use_color   = True
            is_review_required = False
            _gate_forced_color = True
            reasoning_trace.append(
                f"VIG-COLOR (v23): propagation={visual_propagation_score:.2f} ≥ 0.30 + "
                f"feature confirmed (hl={getattr(visual,'highlight_density',0):.4f}, "
                f"density={visual.color_density:.3f}) — "
                "visual instruction feature-matched → COLOR"
            )

        # ── v22: Highlighted-text → hard COLOR forcing ─────────────
        # If highlight pixels overlap dark text strokes, an attorney has annotated
        # actual content on this page — it must print in color.
        # Two tiers (both require ≥ 25% of highlight area covered by text,
        # enforced in visual_analyzer):
        #   Yellow + text  → 0.005 (~0.5% of page ≈ a highlighted sentence)
        #   Any color + text → 0.008 (~0.8% of page — non-yellow needs more area)
        #
        # SEMANTIC GATE: if tfidf_score < 0.05 AND semantic_score < 0.20,
        # the highlighted text contains no case-relevant terms — it's on boilerplate
        # or blank content.  Refuse to promote: the highlight is not the evidence.
        _YELLOW_HL_MIN = 0.001   # ~2 highlighted words at typical scan resolution
        _ALL_HL_MIN    = 0.0015  # non-yellow needs slightly more area
        _hl_yellow = getattr(visual, 'yellow_highlighted_text_density', 0.0)
        _hl_any    = visual.highlighted_text_density
        _hl_area_ok = (_hl_yellow >= _YELLOW_HL_MIN) or (_hl_any >= _ALL_HL_MIN)
        # Semantic gate: highlighted text must be over contextually relevant content
        _hl_sem_ok  = (tfidf_score >= 0.05) or (semantic_score >= 0.20)
        _hl_fires   = _hl_area_ok and _hl_sem_ok
        if (not should_use_color
                and not _hint_force_review
                and _hl_fires):
            _hl_color = "yellow highlight" if _hl_yellow >= _YELLOW_HL_MIN else "highlight"
            _hl_val   = _hl_yellow if _hl_yellow >= _YELLOW_HL_MIN else _hl_any
            decision           = Decision.COLOR
            decision_zone      = "color"
            should_use_color   = True
            is_review_required = False
            _gate_forced_color = True
            reasoning_trace.append(
                f"HIGHLIGHT-TEXT-COLOR (v22): {_hl_color}_text_density="
                f"{_hl_val:.4f} + tfidf={tfidf_score:.3f}/sem={semantic_score:.3f} "
                "— attorney-annotated relevant text → COLOR"
            )
        elif _hl_area_ok and not _hl_sem_ok:
            _hl_val = _hl_yellow if _hl_yellow >= _YELLOW_HL_MIN else _hl_any
            reasoning_trace.append(
                f"HIGHLIGHT suppressed: density={_hl_val:.4f} but tfidf={tfidf_score:.3f} "
                f"and semantic={semantic_score:.3f} — highlighted content has no case relevance"
            )

        # ── v22: Directive visual-feature confirmation → hard COLOR ──
        # The previous page contained a directive ("see red text below",
        # "refer to the photo following") AND this page's visual features
        # confirm the named element actually exists here.
        # A directive without feature confirmation is just a citation boost;
        # a confirmed directive means the content of this page is the specific
        # visual evidence the attorney was pointing to → must print in color.
        if (not should_use_color
                and not _hint_force_review
                and link_info.directive_visual_confirmed):
            decision           = Decision.COLOR
            decision_zone      = "color"
            should_use_color   = True
            is_review_required = False
            _gate_forced_color = True
            _feat_list = ", ".join(link_info.directive_visual_features)
            reasoning_trace.append(
                f"DIRECTIVE-COLOR (v22): previous page instructed viewers here "
                f"and visual feature(s) confirmed on this page [{_feat_list}] → COLOR"
            )

        # ── v22: Directive self-confirmation → hard COLOR ──────────
        # This page itself contains a directive ("see red text below") AND the
        # visual feature it names is confirmed on THIS same page.
        # The source of a directive is itself evidence — it contains the color
        # element it's pointing to AND is instructing the reader to observe it.
        # Guard: require actual visual evidence (ve >= 0.10) to prevent text-only
        # directive phrases like "see attachment highlighted in yellow" from self-confirming
        # on pages that contain no real visual evidence (ve=0.000).
        _directive_self_ve_ok = visual.visual_evidence_score >= 0.10
        if (not should_use_color
                and not _hint_force_review
                and link_info.directive_self_confirmed
                and _directive_self_ve_ok):
            decision           = Decision.COLOR
            decision_zone      = "color"
            should_use_color   = True
            is_review_required = False
            _gate_forced_color = True
            _sf_list = ", ".join(link_info.directive_self_features)
            reasoning_trace.append(
                f"DIRECTIVE-SELF-COLOR (v22): this page directs readers to a visual "
                f"feature [{_sf_list}] that is also confirmed here → COLOR"
            )
        elif (not should_use_color
                and link_info.directive_self_confirmed
                and not _directive_self_ve_ok):
            reasoning_trace.append(
                f"DIRECTIVE-SELF-COLOR skipped: directive references visual feature but "
                f"visual_evidence_score={visual.visual_evidence_score:.3f} < 0.10 "
                f"(text-only reference, no confirmed visual on this page)"
            )

        # ── v23: MEANINGFUL-COLOR gate ────────────────────────────
        # The visual analyzer confirmed meaningful color in the body of the page AND
        # there is a confirmed highlight signal (highlight_density or highlighted_text_density).
        # The highlight requirement prevents template/header color (which sets
        # is_color_meaningful=True but has no text-overlay signal) from triggering.
        # Covers highlight-rich pages that narrowly miss the text-density threshold.
        _MEANINGFUL_MIN = 0.015
        _has_hl_signal  = (
            getattr(visual, 'highlight_density', 0.0) >= 0.001
            or getattr(visual, 'highlighted_text_density', 0.0) >= 0.0005
        )
        if (not should_use_color
                and not _hint_force_review
                and visual.is_color_meaningful
                and visual.color_density >= _MEANINGFUL_MIN
                and _has_hl_signal):
            decision           = Decision.COLOR
            decision_zone      = "color"
            should_use_color   = True
            is_review_required = False
            _gate_forced_color = True
            reasoning_trace.append(
                f"MEANINGFUL-COLOR (v23): visual analyzer confirmed meaningful color "
                f"in body (density={visual.color_density:.3f} ≥ {_MEANINGFUL_MIN}) → COLOR"
            )

        # ── v23: WEAK-EVIDENCE-DEMOTE gate ───────────────────────
        # A page was scored COLOR by the pertinence engine (cross-reference or
        # semantic boost) but has essentially no visual evidence and no case-relevant
        # text.  Without a hard gate to anchor the COLOR decision we demote to B/W.
        # Guard: only fires when no hard gate has already forced COLOR and no
        # directive/VIG confirmation exists.
        _ref_only_color = (
            should_use_color
            and not _gate_forced_color
            and not link_info.directive_visual_confirmed
            and not link_info.directive_self_confirmed
            and visual_score  < 0.005   # virtually no visual evidence signal
            and tfidf_score   < 0.10    # low semantic relevance in document context
            # Note: color_density check removed — a page with density=0.03 but
            # is_color_meaningful=False (template/header color) should still be demoted
            # when no visual evidence signal and low tfidf confirm it's genuine evidence.
        )
        if _ref_only_color:
            decision           = Decision.BW
            decision_zone      = "bw"
            should_use_color   = False
            is_review_required = False
            reasoning_trace.append(
                f"WEAK-EVIDENCE-DEMOTE (v23): COLOR from refs/semantic alone — "
                f"no visual backing (visual={visual_score:.4f}, "
                f"density={visual.color_density:.3f}, tfidf={tfidf_score:.3f}) → B/W"
            )

        # Apply hint "review" zone override after zone decision
        if _hint_force_review:
            decision       = Decision.REVIEW_REQUIRED
            decision_zone  = "review_required"
            should_use_color  = False
            is_review_required = True

        # Special case: page appears grayscale but has strong evidence references
        is_color_sparse = visual.color_density < 0.08 and not visual.is_color_meaningful
        if should_use_color and is_color_sparse:
            reasoning_trace.append(
                "NOTE: Although this page appears near-grayscale, it contains referenced "
                "evidence — importance is based on legal relevance, not visual color presence"
            )

        # Human-readable dominant factor explanation (Case Explanation panel)
        dominant_factor_text = self._get_dominant_factor_text(
            pertinence_result.dominant_factor, visual, semantic, page_role,
            incoming_refs, effective_pertinence, should_use_color, is_review_required,
            link_info=link_info, prior_directive_count=prior_directive_count,
            reasoning_chain=reasoning_chain,
            gate_forced_color=_gate_forced_color,
            reasoning_trace=reasoning_trace,
        )

        # Legal reasoning text — paralegal-style narrative (v23: gate-aware)
        reasoning = self._generate_legal_reasoning(
            page_id=page_idx,
            decision=should_use_color,
            page_role=page_role,
            visual=visual,
            semantic=semantic,
            visual_score=visual_score,
            semantic_score=semantic_score,
            cluster_type=cluster_type,
            cluster_importance=cluster_importance,
            override_triggered=override_triggered and validated_override,
            override_reason=pertinence_result.override_reason,
            tfidf_top_terms=tfidf_top_terms,
            tfidf_score=tfidf_score,
            dominant_factor_text=dominant_factor_text,
            is_review_required=is_review_required,
            link_info=link_info,
            prior_directive_count=prior_directive_count,
            reasoning_chain=reasoning_chain,
            reasoning_trace=reasoning_trace,
            gate_forced_color=_gate_forced_color,
        )

        result = DecisionResult(
            page_id=page_idx,
            should_use_color=should_use_color,
            confidence=confidence,
            final_score=final_score,
            explanation={
                'visual': {
                    'color_density': visual.color_density,
                    'photos': visual.photo_regions,
                    'charts': visual.chart_regions,
                    'is_meaningful': visual.is_color_meaningful,
                    'score': visual_score,
                    'visual_evidence_score': visual.visual_evidence_score,
                    'region_types': [rc.element_type for rc in visual.region_classifications],
                },
                'semantic': {
                    'tfidf_score': tfidf_score,
                    'keyword_importance': kw_importance,
                    'top_terms': tfidf_top_terms,
                    'fuzzy_raw': semantic.fuzzy_match_score,
                    'exhibit_mentions': semantic.exhibit_mentions,
                    'score': semantic_score,
                },
                'context': {
                    'cluster_id': cluster_id,
                    'cluster_type': cluster_type,
                    'cluster_importance': cluster_importance,
                    'page_role': page_role,
                    'incoming_refs': incoming_refs,
                    'is_exhibit_resolution': is_exhibit_res,
                    'ref_strength': ref_strength,
                    'case_type': self._detected_case_type,
                    'case_alignment': case_alignment,
                    # v16
                    'important_visual_types': self._important_visual_types,
                    'cross_page_reference': incoming_refs > 0 or is_exhibit_res,
                },
                'override': {
                    'triggered': override_triggered,
                    'validated': validated_override,
                    'reason': pertinence_result.override_reason or override_reason,
                },
                'pertinence': {
                    'score': pertinence_score,
                    'dominant_factor': pertinence_result.dominant_factor,
                },
                'dominance': {
                    'signal': dominant_signal,
                    'value': dominant_val,
                },
                'conflict': {
                    'type': conflict_type,
                    'penalty': conflict_penalty,
                },
            },
            is_override=(override_triggered and validated_override) or hint_result.hint_applied,
            override_reason=" | ".join(filter(None, [
                pertinence_result.override_reason if validated_override else "",
                hint_result.override_reason if hint_result.hint_applied else "",
            ])),
            page_role=page_role,
            cluster_id=cluster_id,
            cluster_type=cluster_type,
            tfidf_similarity_score=tfidf_score,
            visual_score=visual_score,
            semantic_score=semantic_score,
            reasoning=reasoning,
            pertinence_score=pertinence_score,
            reasoning_trace=reasoning_trace,
            dominant_signal=dominant_signal,
            conflict_type=conflict_type,
            decision_zone=decision_zone,
            is_review_required=is_review_required,
            dominant_factor_text=dominant_factor_text,
            # v16/v17 case-aware fields
            case_type=self._detected_case_type,
            important_visual_types=list(self._important_visual_types),
            cross_page_reference=(incoming_refs > 0 or is_exhibit_res),
            case_visual_match_score=round(case_visual_match, 4),
            propagation_boost=0.0,   # v18: populated by _propagate_evidence_scores()
            # v19: legal arbitration
            authority_weight=arb_result.authority_weight,
            priority_level=arb_result.priority_level,
            arbitration_justification=arb_result.justification,
            conflict_resolved=arb_result.conflict_resolved,
            # v23: gate protection flag
            gate_forced_color=_gate_forced_color,
        )

        # Build page_ctx entry for BERT refinement pass
        page_ctx_entry = {
            'visual': visual, 'semantic': semantic,
            'page_role': page_role,
            'cluster_id': cluster_id, 'cluster_type': cluster_type,
            'cluster_importance': cluster_importance,
            'tfidf_score': tfidf_score, 'tfidf_top_terms': tfidf_top_terms,
            'kw_importance': kw_importance,
            'incoming_refs': incoming_refs, 'is_exhibit_res': is_exhibit_res,
            'ref_strength': ref_strength,
            'rolling_ctx_score': rolling_ctx_score,           # DCE
            'prior_directive_count': prior_directive_count,   # DCE
            'graph_importance': graph_importance,             # full graph (explanations)
            'decision_importance': decision_importance,       # strict graph (scoring)
            'reasoning_chain': reasoning_chain,               # evidence chain strings
            'visual_propagation_score': visual_propagation_score,  # VIG cross-page visual
            'visual_score': visual_score, 'visual_priorities': visual_priorities,
            'case_alignment': case_alignment,
            'override_triggered': override_triggered, 'override_reason': override_reason,
            'validated_override': validated_override,
            'conflict_type': conflict_type, 'conflict_penalty': conflict_penalty,
            'reference_score': reference_score,
            'dominant_val': dominant_val,
            'hint_force_review': _hint_force_review,
        }

        return result, page_ctx_entry

    # ============================================================
    # v18: CROSS-PAGE SCORE PROPAGATION
    # ============================================================

    # Propagation weight: how much citing-page quality can boost a cited page.
    # 0.15 = nudge — strong evidence chain can add up to ~0.15 to final_score.
    _PROPAGATION_WEIGHT   = 0.15
    _PROPAGATION_MIN_BOOST = 0.03   # ignore sub-threshold boosts (noise floor)
    _PROPAGATION_MAX_BOOST = 0.20   # cap so citations alone can't flip a confident B/W

    def _propagate_evidence_scores(self, results: List["DecisionResult"]) -> List["DecisionResult"]:
        """
        v18: Propagate scores from citing pages to cited pages.

        Algorithm (single forward pass):
          For each cited page C (incoming_ref_count > 0):
            1. Collect (citing_page_score, intent_weight) for all pages that
               reference C, where intent_weight comes from EvidenceLinkGraph
               (exhibit reference = 1.0, page reference = 0.70).
            2. Compute weighted average citing quality:
               avg_citing = sum(score_i * weight_i) / sum(weight_i)
            3. Boost = avg_citing * reference_strength_C * PROPAGATION_WEIGHT
            4. If boost > MIN_BOOST: apply to final_score, log to reasoning_trace.
            5. Re-evaluate decision zone with updated score.

        Does NOT bypass FINAL DECISION LAW — propagation runs before it.
        """
        if not results:
            return results

        propagation_log = []

        for i, result in enumerate(results):
            link_info = self.evidence_linker.get_link_info(i)

            if link_info.incoming_ref_count == 0:
                continue  # not cited — nothing to propagate

            citing_pages = link_info.referenced_by   # page indices that cite this page
            if not citing_pages:
                continue

            # Gather weighted citing scores
            # Intent weights from the link graph (exhibit=1.0, page=0.70)
            # If we don't have per-source intent, use reference_strength as a proxy
            ref_strength = link_info.reference_strength   # weighted avg intent of all incoming refs

            weighted_scores = []
            for citing_idx in citing_pages:
                if 0 <= citing_idx < len(results):
                    citing_score = results[citing_idx].final_score
                    # Use reference_strength as the intent weight for all citing pages
                    # (more precise per-source weights would require richer link data)
                    weighted_scores.append(citing_score * ref_strength)

            if not weighted_scores:
                continue

            avg_citing_quality = sum(weighted_scores) / len(weighted_scores)
            boost = round(avg_citing_quality * self._PROPAGATION_WEIGHT, 4)

            # Also give extra weight if this page is an exhibit resolution
            if link_info.is_exhibit_resolution:
                boost = round(boost * 1.30, 4)   # exhibit bodies get 30% extra propagation

            if boost < self._PROPAGATION_MIN_BOOST:
                continue   # sub-noise boost — skip

            boost = min(self._PROPAGATION_MAX_BOOST, boost)

            old_score = result.final_score
            new_score = self._clamp(old_score + boost)
            old_zone  = result.decision_zone

            trace_entry = (
                f"Score Propagation (v18): cited by {len(citing_pages)} page(s) "
                f"[{', '.join(str(p) for p in citing_pages)}]  "
                f"avg_citing_quality={avg_citing_quality:.3f}  "
                f"ref_strength={ref_strength:.2f}  "
                f"boost=+{boost:.4f}  "
                f"score {old_score:.4f} → {new_score:.4f}"
            )
            if link_info.is_exhibit_resolution:
                trace_entry += f"  [exhibit_resolution: 30% amplification applied]"

            # Update score only — decision zone is locked by the conflict gate
            # and must not be overridden by citation boosts.
            result.final_score       = new_score
            result.propagation_boost = boost
            result.reasoning_trace.append(trace_entry)

            propagation_log.append(
                f"  Page {i}: score {old_score:.4f} → {new_score:.4f} "
                f"(boost=+{boost:.4f}, zone locked: {old_zone.upper()})"
            )

        # ── Reverse propagation: boost citing pages that reference high-value pages ──
        # Forward pass (above) boosts CITED pages when their citers score well.
        # Reverse pass boosts CITING pages when they reference a high-value exhibit.
        # Rationale: if exhibit 10 is proven important (high score), every page
        # that argues "see exhibit 10" is likewise material to the case.
        # This turns cross-page references into binding logic, not just additive noise.
        _REVERSE_THRESHOLD  = 0.55   # cited page must score above this to trigger
        _REVERSE_WEIGHT     = 0.12   # citing page gets 12% of cited page's score
        _REVERSE_MAX_BOOST  = 0.15

        for cited_idx, cited_result in enumerate(results):
            if cited_result.final_score < _REVERSE_THRESHOLD:
                continue
            cited_link = self.evidence_linker.get_link_info(cited_idx)
            if not cited_link.referenced_by:
                continue
            for citing_idx in cited_link.referenced_by:
                if not (0 <= citing_idx < len(results)):
                    continue
                citing_result = results[citing_idx]
                # Only boost citing pages that aren't already COLOR
                if citing_result.should_use_color:
                    continue
                raw_boost = cited_result.final_score * _REVERSE_WEIGHT
                # Exhibit resolutions warrant stronger reverse signal
                if cited_link.is_exhibit_resolution:
                    raw_boost *= 1.40
                rev_boost = min(_REVERSE_MAX_BOOST, round(raw_boost, 4))
                if rev_boost < self._PROPAGATION_MIN_BOOST:
                    continue

                old_score = citing_result.final_score
                new_score = self._clamp(old_score + rev_boost)
                old_zone  = citing_result.decision_zone

                # Update score only — zone is locked by conflict gate
                citing_result.final_score = new_score
                citing_result.reasoning_trace.append(
                    f"Reverse Propagation: references page {cited_idx} "
                    f"(score={cited_result.final_score:.3f}) — "
                    f"boost=+{rev_boost:.4f}  score {old_score:.4f} → {new_score:.4f}"
                )
                propagation_log.append(
                    f"  Page {citing_idx}: score {old_score:.4f} → {new_score:.4f} "
                    f"(reverse boost=+{rev_boost:.4f}, cites page {cited_idx}, "
                    f"zone locked: {old_zone.upper()})"
                )

        if propagation_log:
            print(f"\n{'='*60}")
            print("SCORE PROPAGATION (v18): evidence chaining results")
            for line in propagation_log:
                print(line)

        return results

    def _extract_text_from_pdf_page(self, page_num: int) -> str:
        """Extract text directly from PDF if available."""
        if not fitz or not self._current_pdf_path:
            return ""
        
        try:
            doc = fitz.open(self._current_pdf_path)
            if page_num < len(doc):
                page = doc.load_page(page_num)
                text = page.get_text()
                doc.close()
                return text
            doc.close()
        except Exception as e:
            pass
        
        return ""
    
    def _get_cluster_id(self, page_idx: int) -> Optional[int]:
        """Get cluster ID for a page."""
        for cid, indices in self.clusters.items():
            if page_idx in indices:
                return cid
        return None
    
    def _get_context(self) -> DocumentContext:
        """Get current document context."""
        if self._current_context is None:
            raise RuntimeError("Context not initialized. Call initialize_case first.")
        return self._current_context
    
    def _build_signal_dict(self, page: PageRepresentation, 
                           ref_att_signals: List[Signal]) -> Dict[SignalType, float]:
        """Build signal dictionary from all sources."""
        signals = {}
        
        # Visual signal
        visual_score = self.visual_analyzer.calculate_visual_score(page.visual_features)
        signals[SignalType.VISUAL] = visual_score
        
        # Semantic signal
        semantic_score = self.semantic_analyzer.calculate_semantic_score(page.semantic_features)
        signals[SignalType.SEMANTIC] = semantic_score
        
        # Role signal
        signals[SignalType.ROLE] = page.role.value
        
        # Reference and attention signals
        for signal in ref_att_signals:
            signals[signal.type] = signal.value
        
        return signals
    
    def _update_page_signals(self, page: PageRepresentation, 
                             signals: Dict[SignalType, float]) -> PageRepresentation:
        """Update page signals from signal dictionary."""
        # Clear existing signals and rebuild
        page.signals = []
        
        for sig_type, value in signals.items():
            page.signals.append(Signal(
                type=sig_type,
                value=value,
                confidence=0.8,
                source_module="CPCEEngine"
            ))
        
        return page
    
    def _calculate_adaptive_threshold(self, base_threshold: float, page: PageRepresentation,
                                      vf: VisualFeatures, semantic: SemanticFeatures) -> float:
        """
        Calculate adaptive threshold based on page content.
        
        Returns lower threshold for pages with meaningful visual content,
        higher threshold for generic legal text pages.
        """
        threshold = base_threshold
        
        # LOWER threshold for visual evidence (photos, charts)
        if vf.photo_regions > 0:
            threshold -= 0.25
        if vf.chart_regions >= 3:
            threshold -= 0.20
        elif vf.chart_regions >= 2:
            threshold -= 0.15
        if vf.chart_regions > 0 and vf.is_color_meaningful:
            threshold -= 0.10
        
        # LOWER threshold for official documents (stamps)
        if vf.stamp_density > 0.001:
            threshold -= 0.15
        
        # LOWER threshold for highlighted clauses
        if vf.highlight_density > 0.005:
            threshold -= 0.10
        
        # RAISE threshold for pages with low semantic signal
        if semantic.fuzzy_match_score < 0.3 and not vf.is_color_meaningful:
            threshold += 0.15
        
        # Clamp threshold
        return max(0.25, min(0.75, threshold))
    
    def _log_decision(self, page: PageRepresentation, signals: Dict[SignalType, float],
                      result: Dict,
                      processing_time_ms: float) -> None:
        """Log decision to audit layer."""
        inputs = {
            "signals": {k.name: v for k, v in signals.items()},
            "visual_features": page.visual_features,
            "semantic_features": page.semantic_features,
            "page_role": page.role.name
        }
        
        intermediate_scores = {
            "visual": signals.get(SignalType.VISUAL, 0.0),
            "semantic": signals.get(SignalType.SEMANTIC, 0.0),
            "role": signals.get(SignalType.ROLE, 0.0),
            "reference": signals.get(SignalType.REFERENCE, 0.0),
            "attention": signals.get(SignalType.ATTENTION, 0.0),
        }
        
        self.audit_logger.log_decision(
            case_id=self._current_context.case_id,
            page_id=page.page_id,
            inputs=inputs,
            intermediate_scores=intermediate_scores,
            final_decision=result.should_use_color,
            reasoning_tree=result.explanation,
            processing_time_ms=processing_time_ms
        )
    
    def get_audit_report(self, case_id: str = None) -> Dict[str, Any]:
        """Get audit report for a case."""
        if case_id is None:
            if self._current_context is None:
                return {"error": "No case specified and no current context"}
            case_id = self._current_context.case_id
        
        return self.audit_logger.generate_report(case_id)
    
    def is_high_risk_page(self, page_id: int) -> bool:
        """Check if page is high-risk."""
        if self._current_context is None:
            return False
        return self._current_context.is_high_risk_page(page_id)
