"""
CPCE v5 - Reference & Attention Engine
Per specification sections 6 and 7.
"""
import numpy as np
from typing import List, Dict, Tuple
from collections import defaultdict

from .models import PageRepresentation, DocumentContext, Signal, SignalType, CPCEConfig


class ReferenceAttentionEngine:
    """
    Tracks cross-references and attention patterns across pages.
    Implements reference signal per specification section 3.
    """
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
    
    def calculate_reference_score(self, page: PageRepresentation, context: DocumentContext) -> float:
        """
        Calculate reference signal per specification:
        reference_score = (exhibit_mentions * 0.5) + (cross_references * 0.5)
        """
        sf = page.semantic_features
        
        # Base score from exhibit mentions and cross-references
        exhibit_score = min(sf.exhibit_mentions * 0.1, 0.5)  # Cap at 0.5
        cross_ref_score = min(sf.cross_references * 0.05, 0.5)  # Cap at 0.5
        
        # Normalize
        reference_score = exhibit_score + cross_ref_score
        
        # Boost if page is referenced by others
        incoming_refs = self._count_incoming_references(page.page_id, context)
        incoming_boost = min(incoming_refs * 0.05, 0.3)
        
        total_score = min(reference_score + incoming_boost, 1.0)
        return float(total_score)
    
    def calculate_attention_score(self, page: PageRepresentation, context: DocumentContext) -> float:
        """
        Calculate attention signal per specification:
        attention_score = neighbor_boost + repetition_strength
        """
        neighbor_boost = self._calculate_neighbor_boost(page, context)
        repetition_strength = self._calculate_repetition_strength(page, context)
        
        # Combine with weights
        attention_score = 0.6 * neighbor_boost + 0.4 * repetition_strength
        
        return float(min(attention_score, 1.0))
    
    def _count_incoming_references(self, page_id: int, context: DocumentContext) -> int:
        """Count how many pages reference this page."""
        count = 0
        for other_page in context.pages:
            if other_page.page_id == page_id:
                continue
            # Check if this page mentions the target
            text = other_page.text.lower()
            patterns = [f'page {page_id + 1}', f'page {page_id}']
            if any(p in text for p in patterns):
                count += 1
        return count
    
    def _calculate_neighbor_boost(self, page: PageRepresentation, context: DocumentContext) -> float:
        """
        Calculate attention boost from neighboring pages.
        If neighbors are important, this page may be important too.
        """
        page_id = page.page_id
        total_pages = len(context.pages)
        
        if total_pages <= 1:
            return 0.0
        
        boost = 0.0
        neighbor_count = 0
        
        # Check previous page
        if page_id > 0:
            prev_page = context.pages[page_id - 1]
            boost += prev_page.importance_score
            neighbor_count += 1
        
        # Check next page
        if page_id < total_pages - 1:
            next_page = context.pages[page_id + 1]
            boost += next_page.importance_score
            neighbor_count += 1
        
        if neighbor_count > 0:
            return min(boost / neighbor_count, 1.0)
        return 0.0
    
    def _calculate_repetition_strength(self, page: PageRepresentation, context: DocumentContext) -> float:
        """
        Calculate how often this page's content is repeated/referenced.
        """
        page_id = page.page_id
        
        # Check attention graph
        if page_id not in context.attention_graph:
            return 0.0
        
        connections = len(context.attention_graph[page_id])
        total_pages = len(context.pages)
        
        if total_pages <= 1:
            return 0.0
        
        # Normalize by total pages
        repetition_strength = connections / (total_pages - 1)
        return min(repetition_strength, 1.0)
    
    def build_attention_signals(self, page: PageRepresentation, context: DocumentContext) -> List[Signal]:
        """Build reference and attention signals for a page."""
        signals = []
        
        # Reference signal
        ref_score = self.calculate_reference_score(page, context)
        signals.append(Signal(
            type=SignalType.REFERENCE,
            value=ref_score,
            confidence=0.8,
            source_module="ReferenceAttentionEngine",
            metadata={
                "exhibit_mentions": page.semantic_features.exhibit_mentions,
                "cross_references": page.semantic_features.cross_references
            }
        ))
        
        # Attention signal
        att_score = self.calculate_attention_score(page, context)
        signals.append(Signal(
            type=SignalType.ATTENTION,
            value=att_score,
            confidence=0.7,
            source_module="ReferenceAttentionEngine",
            metadata={
                "neighbor_boost": self._calculate_neighbor_boost(page, context),
                "repetition_strength": self._calculate_repetition_strength(page, context)
            }
        ))
        
        return signals
