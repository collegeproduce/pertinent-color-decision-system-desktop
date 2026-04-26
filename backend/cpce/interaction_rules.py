"""
CPCE v5 - Interaction Rules Engine and Contradiction Handler
Per specification sections 8 and 9.
"""
import numpy as np
from typing import List, Dict, Tuple, Any
from dataclasses import dataclass

from .models import PageRepresentation, Signal, SignalType, CPCEConfig, DecisionExplanation


@dataclass
class InteractionRule:
    """A rule for signal interaction."""
    name: str
    condition: str
    action: str
    priority: int


class InteractionRulesEngine:
    """
    Defines interaction rules between signals.
    Per specification section 8.
    """
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
        self.rules = self._load_rules()
    
    def _load_rules(self) -> List[InteractionRule]:
        """Load default interaction rules."""
        return [
            InteractionRule(
                name="visual_semantic_agreement",
                condition="visual_high AND semantic_high",
                action="boost_both",
                priority=1
            ),
            InteractionRule(
                name="visual_semantic_conflict",
                condition="visual_high AND semantic_low",
                action="penalize_visual",
                priority=2
            ),
            InteractionRule(
                name="role_boost",
                condition="role_is_exhibit OR role_is_evidence",
                action="boost_all",
                priority=1
            ),
            InteractionRule(
                name="narrative_penalty",
                condition="role_is_narrative",
                action="penalize_all",
                priority=3
            ),
        ]
    
    def apply_rules(self, page: PageRepresentation, signals: Dict[SignalType, float]) -> Dict[SignalType, float]:
        """
        Apply interaction rules to signal scores.
        """
        modified_signals = signals.copy()
        
        # Get scores
        visual_score = signals.get(SignalType.VISUAL, 0.0)
        semantic_score = signals.get(SignalType.SEMANTIC, 0.0)
        role_score = signals.get(SignalType.ROLE, 0.0)
        
        # Rule: visual_semantic_agreement
        if visual_score > 0.5 and semantic_score > 0.5:
            # Both high - boost both
            modified_signals[SignalType.VISUAL] = min(visual_score * 1.15, 1.0)
            modified_signals[SignalType.SEMANTIC] = min(semantic_score * 1.15, 1.0)
        
        # Rule: visual_semantic_conflict
        elif visual_score > 0.5 and semantic_score < 0.3:
            # Visual high but semantic low - penalize visual
            modified_signals[SignalType.VISUAL] = visual_score * 0.7
        
        # Rule: role_boost
        if role_score >= 0.7:  # EXHIBIT or EVIDENCE
            for sig_type in [SignalType.VISUAL, SignalType.SEMANTIC]:
                modified_signals[sig_type] = min(modified_signals.get(sig_type, 0.0) * 1.1, 1.0)
        
        # Rule: narrative_penalty
        if role_score <= 0.2:  # NARRATIVE or COVER or INDEX
            for sig_type in modified_signals:
                modified_signals[sig_type] = modified_signals[sig_type] * 0.8
        
        return modified_signals


class ContradictionHandler:
    """
    Detects and handles contradictions between signals.
    Per specification section 8.
    """
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
        self.threshold = 0.4  # Minimum difference to flag as contradiction
    
    def detect_contradictions(self, signals: Dict[SignalType, float]) -> List[Dict[str, Any]]:
        """
        Detect contradictions between signals.
        """
        contradictions = []
        
        # Check visual vs semantic contradiction
        visual = signals.get(SignalType.VISUAL, 0.0)
        semantic = signals.get(SignalType.SEMANTIC, 0.0)
        
        if abs(visual - semantic) > self.threshold:
            contradictions.append({
                "type": "visual_semantic_mismatch",
                "severity": abs(visual - semantic),
                "signal_a": "visual",
                "signal_b": "semantic",
                "value_a": visual,
                "value_b": semantic
            })
        
        # Check role vs visual contradiction
        role = signals.get(SignalType.ROLE, 0.0)
        if visual > 0.6 and role < 0.2:
            # High visual score but low role score
            contradictions.append({
                "type": "role_visual_mismatch",
                "severity": visual - role,
                "signal_a": "visual",
                "signal_b": "role",
                "value_a": visual,
                "value_b": role,
                "note": "High visual importance on narrative page"
            })
        
        return contradictions
    
    def calculate_penalty(self, contradictions: List[Dict[str, Any]]) -> float:
        """
        Calculate contradiction penalty per specification section 2.
        """
        if not contradictions:
            return 0.0
        
        # Sum severity scores
        total_severity = sum(c["severity"] for c in contradictions)
        
        # Apply diminishing returns
        penalty = min(total_severity * 0.15, 0.3)
        
        return float(penalty)
    
    def resolve(self, page: PageRepresentation, signals: Dict[SignalType, float]) -> Tuple[Dict[SignalType, float], List[Dict[str, Any]]]:
        """
        Resolve contradictions and return modified signals.
        """
        contradictions = self.detect_contradictions(signals)
        
        if not contradictions:
            return signals, []
        
        modified_signals = signals.copy()
        penalty = self.calculate_penalty(contradictions)
        
        # Apply penalty to all signals proportionally
        for sig_type in modified_signals:
            modified_signals[sig_type] = max(0.0, modified_signals[sig_type] - penalty)
        
        return modified_signals, contradictions
    
    def build_conflict_reasoning(self, contradictions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Build conflict reasoning for decision explanation.
        """
        if not contradictions:
            return {
                "has_conflict": False,
                "conflicts": [],
                "penalty": 0.0
            }
        
        penalty = self.calculate_penalty(contradictions)
        
        return {
            "has_conflict": True,
            "conflicts": contradictions,
            "penalty": penalty,
            "resolution": f"Applied penalty of {penalty:.3f} to all signals"
        }
