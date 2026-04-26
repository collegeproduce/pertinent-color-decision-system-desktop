"""CPCE v8 - Contextual Pertinent Color Engine — Reasoning Edition"""
from .models import (
    CPCEConfig,
    DocumentContext,
    PageRepresentation,
    Signal,
    SignalType,
    PageRole,
    DecisionExplanation
)
from .engine import CPCEEngine, DecisionResult
from .visual_analyzer import VisualAnalyzer
from .semantic_analyzer import SemanticAnalyzer, TFIDFEngine
from .document_context import DocumentContextLayer, PageRoleClassifier
from .ocr_layer import OCRLayer, OCRResult
from .reference_attention_engine import ReferenceAttentionEngine
from .interaction_rules import InteractionRulesEngine, ContradictionHandler
from .relevance_engine import RelevanceEngine, ConfidenceCalibration
from .final_decision_engine import FinalDecisionEngine
from .pertinence_engine import ColorPertinenceEngine, PertinenceResult
from .evidence_linker import EvidenceLinkGraph, PageLinkInfo
from .visual_element_classifier import VisualElementClassifier, RegionClassification, ELEMENT_PERTINENCE
from .legal_bert_engine import LegalBertEngine
from .cross_page_memory import CrossPageMemoryEngine
from .audit_logger import AuditLogger
from .pdf_processor import PDFProcessor, load_image
from .reasoning_graph import LegalReasoningGraph, ReasoningEdge, PageNode
from .visual_instruction_graph import VisualInstructionGraph, VisualInstruction, VisualPropagationEdge
from .arbitration_engine import LegalArbitrationEngine, ArbitrationResult
from .user_hint_engine import UserHintEngine, HintResult

__version__ = "19.0.0"
__all__ = [
    "CPCEEngine",
    "CPCEConfig",
    "DocumentContext",
    "PageRepresentation",
    "Signal",
    "SignalType",
    "PageRole",
    "DecisionExplanation",
    "DecisionResult",
    "VisualAnalyzer",
    "SemanticAnalyzer",
    "TFIDFEngine",
    "DocumentContextLayer",
    "PageRoleClassifier",
    "OCRLayer",
    "OCRResult",
    "ReferenceAttentionEngine",
    "InteractionRulesEngine",
    "ContradictionHandler",
    "RelevanceEngine",
    "ConfidenceCalibration",
    "FinalDecisionEngine",
    "ColorPertinenceEngine",
    "PertinenceResult",
    "EvidenceLinkGraph",
    "PageLinkInfo",
    "VisualElementClassifier",
    "RegionClassification",
    "ELEMENT_PERTINENCE",
    "CrossPageMemoryEngine",
    "AuditLogger",
    "PDFProcessor",
    "load_image",
    "LegalReasoningGraph",
    "ReasoningEdge",
    "PageNode",
    "VisualInstructionGraph",
    "VisualInstruction",
    "VisualPropagationEdge",
    "LegalArbitrationEngine",
    "ArbitrationResult",
    "UserHintEngine",
    "HintResult",
]
