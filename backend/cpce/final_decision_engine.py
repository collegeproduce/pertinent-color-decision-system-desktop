"""
CPCE v5 - Final Decision Engine
Per specification sections 5, 9, 10, and 15.
"""
import numpy as np
from typing import Tuple, List, Dict, Any
from dataclasses import dataclass

from .models import (
    PageRepresentation, DocumentContext, CPCEConfig, 
    DecisionExplanation, Signal, SignalType, PageRole
)
from .relevance_engine import RelevanceEngine, ConfidenceCalibration


@dataclass
class DecisionResult:
    """Result of the final decision."""
    should_use_color: bool
    final_score: float
    confidence: float
    explanation: DecisionExplanation
    is_override: bool
    override_reason: str


class FinalDecisionEngine:
    """
    Makes final decision on whether to use color.
    Implements hard legal overrides and tie-breaking per specification.
    """
    
    # Decision threshold
    COLOR_THRESHOLD = 0.5
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
        self.relevance_engine = RelevanceEngine(config)
        self.confidence_calibrator = ConfidenceCalibration(config)
    
    def decide(self, page: PageRepresentation, context: DocumentContext,
               signals: Dict[SignalType, float], contradictions: List[Dict] = None) -> DecisionResult:
        """
        Make final decision per specification.
        """
        # Check for hard legal overrides per section 5
        is_override, override_reason = self._check_hard_overrides(page, context)
        
        if is_override:
            explanation = self.relevance_engine.build_reasoning_tree(page, signals)
            explanation.decision = True
            explanation.final_score = 1.0
            explanation.confidence = 1.0
            explanation.conflict_reasoning = self._build_conflict_reasoning(contradictions)
            
            return DecisionResult(
                should_use_color=True,
                final_score=1.0,
                confidence=1.0,
                explanation=explanation,
                is_override=True,
                override_reason=override_reason
            )
        
        # Calculate final score
        final_score = self.relevance_engine.calculate_final_score(signals)
        
        # Calculate confidence
        confidence = self.confidence_calibrator.calculate_confidence(signals, contradictions)
        
        # Make decision
        should_use_color = final_score >= self.COLOR_THRESHOLD
        
        # Build explanation tree
        explanation = self.relevance_engine.build_reasoning_tree(page, signals)
        explanation.decision = should_use_color
        explanation.final_score = final_score
        explanation.confidence = confidence
        explanation.conflict_reasoning = self._build_conflict_reasoning(contradictions)
        
        return DecisionResult(
            should_use_color=should_use_color,
            final_score=final_score,
            confidence=confidence,
            explanation=explanation,
            is_override=False,
            override_reason=""
        )
    
    def _check_hard_overrides(self, page: PageRepresentation, context: DocumentContext) -> Tuple[bool, str]:
        """
        Check for hard legal overrides per specification section 5.
        FIXED: Now properly triggers on meaningful visual content.
        """
        vf = page.visual_features
        
        # OVERRIDE 1: Meaningful color with evidence elements
        # Photos are ALWAYS evidence
        if vf.photo_regions > 0:
            return True, f"HARD_OVERRIDE: evidence_photo_detected:{vf.photo_regions}"
        
        # OVERRIDE 2: Multiple charts (financial/medical evidence)
        if vf.chart_regions >= 3:
            return True, f"HARD_OVERRIDE: chart_evidence:{vf.chart_regions}_charts"
        
        # OVERRIDE 3: Stamps/seals on official documents
        if vf.stamp_density > 0.001:
            return True, f"HARD_OVERRIDE: official_stamp:{vf.stamp_density:.4f}"
        
        # OVERRIDE 4: Highlighted key clauses
        if vf.highlight_density > 0.005:
            return True, f"HARD_OVERRIDE: highlighted_clause:{vf.highlight_density:.4f}"
        
        # OVERRIDE 5: Exhibit pages with meaningful color
        if page.role == PageRole.EXHIBIT and vf.is_color_meaningful:
            return True, "HARD_OVERRIDE: exhibit_meaningful_color"
        
        # OVERRIDE 6: Evidence role with ANY meaningful color
        if page.role == PageRole.EVIDENCE and vf.is_color_meaningful:
            return True, "HARD_OVERRIDE: evidence_meaningful_color"
        
        # Check text content for high-risk keywords + ANY color
        text_lower = page.text.lower()
        high_risk_keywords = [
            'injury', 'wound', 'medical', 'x-ray', 'trauma', 'mri', 'ct scan',
            'fracture', 'bruise', 'scar', 'surgery', 'hospital',
            'financial loss', 'damages', 'compensation', 'settlement'
        ]
        
        found_keywords = [kw for kw in high_risk_keywords if kw in text_lower]
        if found_keywords and vf.color_density > 0.02:  # Lowered threshold
            return True, f"HARD_OVERRIDE: keywords {found_keywords} with color"
        
        return False, ""
    
    def _build_conflict_reasoning(self, contradictions: List[Dict]) -> Dict[str, Any]:
        """Build conflict reasoning for explanation."""
        if not contradictions:
            return {"has_conflict": False, "conflicts": [], "penalty": 0.0}
        
        penalty = sum(c.get("severity", 0.0) for c in contradictions) * 0.15
        
        return {
            "has_conflict": True,
            "conflicts": contradictions,
            "penalty": min(penalty, 0.3),
            "resolution": f"Applied penalty of {min(penalty, 0.3):.3f}"
        }
    
    def tie_break(self, page_a: PageRepresentation, page_b: PageRepresentation,
                  signals_a: Dict[SignalType, float], signals_b: Dict[SignalType, float]) -> PageRepresentation:
        """
        Tie-breaking rules per specification section 9:
        1. higher visual_score
        2. higher semantic_score
        3. higher role_score
        4. lower page_index
        """
        vis_a = signals_a.get(SignalType.VISUAL, 0.0)
        vis_b = signals_b.get(SignalType.VISUAL, 0.0)
        
        if vis_a != vis_b:
            return page_a if vis_a > vis_b else page_b
        
        sem_a = signals_a.get(SignalType.SEMANTIC, 0.0)
        sem_b = signals_b.get(SignalType.SEMANTIC, 0.0)
        
        if sem_a != sem_b:
            return page_a if sem_a > sem_b else page_b
        
        role_a = signals_a.get(SignalType.ROLE, 0.0)
        role_b = signals_b.get(SignalType.ROLE, 0.0)
        
        if role_a != role_b:
            return page_a if role_a > role_b else page_b
        
        # Lower page index wins
        return page_a if page_a.page_id < page_b.page_id else page_b
