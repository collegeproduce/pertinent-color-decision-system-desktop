"""
CPCE v5 - Relevance Engine and Confidence Calibration
Per specification sections 4 and 10.
"""
import numpy as np
from typing import List, Dict, Any
from scipy.stats import entropy as shannon_entropy

from .models import PageRepresentation, Signal, SignalType, CPCEConfig, DecisionExplanation


class RelevanceEngine:
    """
    Calculates final relevance score.
    Per specification section 2 (final scoring model).
    """
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
        self.config.normalize_weights()
    
    def calculate_final_score(self, signals: Dict[SignalType, float]) -> float:
        """
        Calculate final score per specification:
        final_score = wv*visual + ws*semantic + wr*role + wc*reference + wa*attention - wd*contradiction
        """
        visual_score = signals.get(SignalType.VISUAL, 0.0)
        semantic_score = signals.get(SignalType.SEMANTIC, 0.0)
        role_score = signals.get(SignalType.ROLE, 0.0)
        ref_score = signals.get(SignalType.REFERENCE, 0.0)
        att_score = signals.get(SignalType.ATTENTION, 0.0)
        
        # Use normalized weights
        wv = self.config.wv
        ws = self.config.ws
        wr = self.config.wr
        wc = self.config.wc
        wa = self.config.wa
        
        final_score = (
            wv * visual_score +
            ws * semantic_score +
            wr * role_score +
            wc * ref_score +
            wa * att_score
        )
        
        # Clamp to [0, 1]
        return float(np.clip(final_score, 0.0, 1.0))
    
    def build_reasoning_tree(self, page: PageRepresentation, signals: Dict[SignalType, float]) -> DecisionExplanation:
        """
        Build decision explanation tree per specification section 14.
        """
        vf = page.visual_features
        sf = page.semantic_features
        
        explanation = DecisionExplanation(
            decision=False,  # Will be set by FinalDecisionEngine
            final_score=0.0,
            confidence=0.0,
            visual_reasoning={
                "color_density": vf.color_density,
                "entropy": vf.entropy,
                "is_meaningful": vf.is_color_meaningful,
                "reason": vf.meaningfulness_reason,
                "spatial_distribution": vf.spatial_distribution,
                "photo_regions": vf.photo_regions,
                "chart_regions": vf.chart_regions,
                "highlight_density": vf.highlight_density,
                "stamp_density": vf.stamp_density,
                "signal_value": signals.get(SignalType.VISUAL, 0.0)
            },
            semantic_reasoning={
                "embedding_similarity": sf.embedding_similarity_score,
                "fuzzy_score": sf.fuzzy_match_score,
                "exhibit_mentions": sf.exhibit_mentions,
                "cross_references": sf.cross_references,
                "key_phrases": sf.key_phrases,
                "signal_value": signals.get(SignalType.SEMANTIC, 0.0)
            },
            role_reasoning={
                "detected_role": page.role.name,
                "role_weight": page.role.value,
                "signal_value": signals.get(SignalType.ROLE, 0.0)
            },
            reference_reasoning={
                "reference_score": signals.get(SignalType.REFERENCE, 0.0)
            },
            conflict_reasoning={}
        )
        
        return explanation


class ConfidenceCalibration:
    """
    Calibrates confidence scores per specification section 4.
    """
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
    
    def calculate_confidence(self, signals: Dict[SignalType, float], contradictions: List[Dict] = None) -> float:
        """
        Calculate confidence per specification:
        confidence = signal_agreement * signal_strength * (1 - entropy) * (1 - conflict_score)
        """
        signal_values = list(signals.values())
        
        if not signal_values:
            return 0.0
        
        # Signal agreement
        signal_agreement = self._calculate_signal_agreement(signal_values)
        
        # Signal strength
        signal_strength = np.mean(signal_values)
        
        # Entropy of score distribution
        entropy = self._calculate_entropy(signal_values)
        
        # Conflict score
        conflict_score = self._calculate_conflict_score(signals)
        if contradictions:
            conflict_score = max(conflict_score, len(contradictions) * 0.1)
        
        # Final confidence
        confidence = (
            signal_agreement *
            signal_strength *
            (1 - entropy) *
            (1 - conflict_score)
        )
        
        return float(np.clip(confidence, 0.0, 1.0))
    
    def _calculate_signal_agreement(self, values: List[float]) -> float:
        """
        Calculate signal agreement as 1 - variance.
        Higher agreement when signals are similar.
        """
        if len(values) <= 1:
            return 1.0
        
        variance = np.var(values)
        # Normalize variance to [0, 1] range
        agreement = max(0.0, 1.0 - variance)
        return float(agreement)
    
    def _calculate_entropy(self, values: List[float]) -> float:
        """
        Calculate Shannon entropy of score distribution.
        """
        if not values:
            return 0.0
        
        # Normalize to probabilities
        total = sum(values) + self.config.epsilon
        probs = [v / total for v in values]
        
        # Calculate entropy
        entropy = shannon_entropy(probs)
        
        # Normalize to [0, 1] (max entropy for n values is log(n))
        max_entropy = np.log(len(values)) if len(values) > 1 else 1.0
        normalized_entropy = entropy / max_entropy if max_entropy > 0 else 0.0
        
        return float(normalized_entropy)
    
    def _calculate_conflict_score(self, signals: Dict[SignalType, float]) -> float:
        """
        Calculate conflict score as absolute difference between visual and semantic.
        """
        visual = signals.get(SignalType.VISUAL, 0.0)
        semantic = signals.get(SignalType.SEMANTIC, 0.0)
        
        return float(abs(visual - semantic))
