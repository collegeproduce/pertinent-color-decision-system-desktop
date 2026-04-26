"""
CPCE v5 - Core Data Models
Signal and PageRepresentation schemas as per specification.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Tuple
from enum import Enum, auto
import numpy as np
from datetime import datetime


class SignalType(Enum):
    VISUAL = auto()
    SEMANTIC = auto()
    ROLE = auto()
    REFERENCE = auto()
    ATTENTION = auto()


class PageRole(Enum):
    EVIDENCE = 1.0      # Color likely legally meaningful
    NARRATIVE = 0.1     # Color likely decorative
    SIGNATURE = 0.7     # Color possibly meaningful
    COVER = 0.0         # Color decorative
    INDEX = 0.0         # Color decorative
    EXHIBIT = 1.0       # Color legally meaningful
    UNKNOWN = 0.5       # Undetermined


@dataclass
class Signal:
    """
    Signal data schema per specification section 11.
    """
    type: SignalType
    value: float
    confidence: float
    source_module: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def __post_init__(self):
        # Numerical stability per section 10
        epsilon = 1e-6
        self.value = float(np.clip(self.value, 0 + epsilon, 1 - epsilon))
        self.confidence = float(np.clip(self.confidence, 0 + epsilon, 1 - epsilon))


@dataclass
class VisualFeatures:
    """OpenCV-extracted visual features per specification."""
    color_density: float = 0.0
    highlight_density: float = 0.0
    stamp_density: float = 0.0
    entropy: float = 0.0
    header_fraction: float = 0.0
    body_fraction: float = 0.0
    spatial_distribution: float = 0.0
    is_color_meaningful: bool = False
    meaningfulness_reason: str = ""
    photo_regions: int = 0
    chart_regions: int = 0
    detected_elements: Dict[str, Any] = field(default_factory=dict)
    # v10: region-level data for the Visual Element Classifier
    photo_region_data: List[Dict] = field(default_factory=list)
    chart_region_data: List[Dict] = field(default_factory=list)
    region_classifications: List[Any] = field(default_factory=list)   # List[RegionClassification]
    visual_evidence_score: float = 0.0   # aggregate impact-weighted confidence
    # v19: non-color visual intelligence (shape-based, saturation-agnostic)
    grayscale_regions: int = 0            # B&W photos, scanned exhibits
    grayscale_region_data: List[Dict] = field(default_factory=list)
    signature_regions: int = 0            # handwritten signature clusters
    signature_region_data: List[Dict] = field(default_factory=list)
    bw_stamp_regions: int = 0             # B&W notary seals, court stamps
    bw_stamp_region_data: List[Dict] = field(default_factory=list)
    # v20: colored annotation text — red/orange attorney markings in body text
    # (not stamps, not highlights, not photos — distributed text-like color)
    colored_annotation_density: float = 0.0
    # v22: highlight pixels that overlap dark text strokes — confirms highlighted text vs empty color
    highlighted_text_density: float = 0.0
    # v22: yellow-only highlighted text density (stricter — yellow is the legal annotation standard)
    yellow_highlighted_text_density: float = 0.0


@dataclass
class SemanticFeatures:
    """Semantic features from Legal-BERT and fuzzy matching."""
    embedding: Optional[np.ndarray] = None
    embedding_similarity_score: float = 0.0
    fuzzy_match_score: float = 0.0
    semantic_score: float = 0.0
    key_phrases: List[str] = field(default_factory=list)
    exhibit_mentions: int = 0
    cross_references: int = 0


@dataclass
class PageRepresentation:
    """
    Page representation per specification section 12.
    """
    page_id: int
    text: str = ""
    visual_features: VisualFeatures = field(default_factory=VisualFeatures)
    semantic_features: SemanticFeatures = field(default_factory=SemanticFeatures)
    signals: List[Signal] = field(default_factory=list)
    role: PageRole = PageRole.UNKNOWN
    importance_score: float = 0.0
    confidence: float = 0.0
    raw_image: Optional[np.ndarray] = None
    
    # Audit fields
    decision_tree: Dict[str, Any] = field(default_factory=dict)
    processing_timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DocumentContext:
    """Document-level context for case isolation."""
    case_id: str
    document_type: str = ""
    total_pages: int = 0
    pages: List[PageRepresentation] = field(default_factory=list)
    exhibit_index: Dict[str, int] = field(default_factory=dict)
    attention_graph: Dict[int, List[int]] = field(default_factory=dict)
    high_risk_flags: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DecisionExplanation:
    """Decision explanation tree per specification section 14."""
    decision: bool
    visual_reasoning: Dict[str, Any] = field(default_factory=dict)
    semantic_reasoning: Dict[str, Any] = field(default_factory=dict)
    role_reasoning: Dict[str, Any] = field(default_factory=dict)
    reference_reasoning: Dict[str, Any] = field(default_factory=dict)
    conflict_reasoning: Dict[str, Any] = field(default_factory=dict)
    final_score: float = 0.0
    confidence: float = 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "final_score": float(self.final_score),
            "confidence": float(self.confidence),
            "reasoning": {
                "visual": self.visual_reasoning,
                "semantic": self.semantic_reasoning,
                "role": self.role_reasoning,
                "reference": self.reference_reasoning,
                "conflict": self.conflict_reasoning,
            }
        }


@dataclass
class CPCEConfig:
    """Configuration for CPCE engine with deterministic defaults."""
    # Weight configuration per section 2
    wv: float = 0.25  # visual weight
    ws: float = 0.25  # semantic weight
    wr: float = 0.20  # role weight
    wc: float = 0.15  # reference weight
    wa: float = 0.15  # attention weight
    
    # Semantic constants per section 3
    alpha: float = 0.7  # embedding similarity weight
    beta: float = 0.3   # fuzzy score weight
    
    # Color detection thresholds per section 1
    saturation_threshold: int = 35
    header_zone_percent: float = 0.12
    min_color_density: float = 0.08
    rich_color_density: float = 0.15
    moderate_entropy: float = 3.8
    rich_entropy: float = 4.5
    
    # Numerical stability per section 10
    epsilon: float = 1e-6
    dtype: str = "float64"
    
    # Performance per section 15
    target_ms_per_page: int = 100
    max_workers: int = 4
    
    # High risk keywords per section 5
    high_risk_keywords: List[str] = field(default_factory=lambda: [
        "injury", "wound", "damage", "medical", "x-ray",
        "financial loss", "chart", "exhibit", "evidence",
        "photograph", "image", "diagram", "blood"
    ])
    
    def normalize_weights(self) -> None:
        """Renormalize weights per section 2."""
        total = self.wv + self.ws + self.wr + self.wc + self.wa
        if total > 0:
            self.wv /= total
            self.ws /= total
            self.wr /= total
            self.wc /= total
            self.wa /= total
