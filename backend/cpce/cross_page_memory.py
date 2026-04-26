"""
CPCE v5 - Cross-Page Memory Engine
Tracks references and concepts across the entire document to enable
context-aware reasoning (e.g., "Page 10 matters because Page 3 referenced it").
"""
from typing import List, Dict, Set, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict
import re


@dataclass
class PageReference:
    """A reference from one page to another or to a concept."""
    source_page: int
    target_type: str  # 'page', 'exhibit', 'concept', 'entity'
    target_id: str    # page number, exhibit letter, concept name
    reference_text: str
    confidence: float


@dataclass
class GlobalMemory:
    """Global context memory across all pages."""
    # Entities mentioned (people, organizations, case numbers)
    entities: Dict[str, Dict] = field(default_factory=dict)

    # Concepts tracked (injury, liability, damages, etc.)
    concepts: Dict[str, List[int]] = field(default_factory=lambda: defaultdict(list))

    # Page-to-page references
    references: List[PageReference] = field(default_factory=list)

    # Exhibit tracking
    exhibits: Dict[str, Dict] = field(default_factory=dict)

    # Running importance scores per page
    page_importance: Dict[int, float] = field(default_factory=dict)

    # Rolling context: short text summaries per page for forward reasoning
    page_summaries: Dict[int, str] = field(default_factory=dict)


class CrossPageMemoryEngine:
    """
    Maintains global memory across all pages to enable context-aware decisions.
    
    Key capability: If Page 3 says "See Exhibit A for accident scene"
    and Page 10 contains the accident scene photo,
    then Page 10's importance is boosted.
    """
    
    REFERENCE_PATTERNS = [
        # Page references
        r'see\s+page\s+(\d+)',
        r'refer\s+to\s+page\s+(\d+)',
        r'\(page\s+(\d+)\)',
        # Exhibit references
        r'see\s+exhibit\s+([A-Z0-9-]+)',
        r'refer\s+to\s+exhibit\s+([A-Z0-9-]+)',
        r'see\s+exh\.?\s*([A-Z0-9-]+)',
        # Attachment references
        r'see\s+attachment\s+([A-Z0-9-]+)',
        # Forward/backward references
        r'as\s+discussed\s+(?:on\s+)?page\s+(\d+)',
        r'as\s+shown\s+(?:on\s+)?page\s+(\d+)',
    ]
    
    KEY_CONCEPTS = [
        'injury', 'liability', 'damages', 'negligence', 'breach',
        'contract', 'agreement', 'accident', 'incident', 'evidence',
        'witness', 'testimony', 'expert', 'medical', 'financial'
    ]
    
    def __init__(self):
        self.memory = GlobalMemory()
        self.current_page = 0
    
    def process_page(self, page_id: int, text: str, 
                     exhibit_mentions: List[str]) -> Dict:
        """
        Process a page and update global memory.
        
        Returns:
            Dict with references found and boosts to apply
        """
        self.current_page = page_id
        
        # Find references to other pages
        page_refs = self._extract_page_references(page_id, text)
        
        # Store page summary for rolling context
        self.memory.page_summaries[page_id] = text[:250].strip() if text else ""

        # Find concept mentions
        concept_mentions = self._extract_concepts(text)

        # Track exhibits
        for exhibit in exhibit_mentions:
            self._track_exhibit(exhibit, page_id)
        
        # Update memory
        for ref in page_refs:
            self.memory.references.append(ref)
        
        for concept in concept_mentions:
            self.memory.concepts[concept].append(page_id)
        
        # Calculate boosts for this page
        boosts = self._calculate_page_boosts(page_id, text, exhibit_mentions)
        
        return {
            'page_refs': page_refs,
            'concepts': concept_mentions,
            'boosts': boosts
        }
    
    def _extract_page_references(self, page_id: int, text: str) -> List[PageReference]:
        """Extract references to other pages or exhibits."""
        references = []
        text_lower = text.lower()
        
        for pattern in self.REFERENCE_PATTERNS:
            matches = re.finditer(pattern, text_lower)
            for match in matches:
                target = match.group(1)
                ref_text = match.group(0)
                
                # Determine target type
                if 'exhibit' in ref_text or 'exh' in ref_text:
                    target_type = 'exhibit'
                elif 'attachment' in ref_text:
                    target_type = 'attachment'
                else:
                    target_type = 'page'
                
                references.append(PageReference(
                    source_page=page_id,
                    target_type=target_type,
                    target_id=target,
                    reference_text=ref_text,
                    confidence=0.9
                ))
        
        return references
    
    def _extract_concepts(self, text: str) -> List[str]:
        """Extract key legal concepts from text."""
        text_lower = text.lower()
        found = []
        
        for concept in self.KEY_CONCEPTS:
            if concept in text_lower:
                found.append(concept)
        
        return found
    
    def _track_exhibit(self, exhibit_id: str, page_id: int):
        """Track where exhibits are mentioned."""
        if exhibit_id not in self.memory.exhibits:
            self.memory.exhibits[exhibit_id] = {
                'first_mention': page_id,
                'mentions': [],
                'resolved': False
            }
        
        self.memory.exhibits[exhibit_id]['mentions'].append(page_id)
    
    def _calculate_page_boosts(self, page_id: int, text: str, 
                               exhibit_mentions: List[str]) -> Dict[str, float]:
        """
        Calculate importance boosts for this page based on global context.
        """
        boosts = {}
        
        # Boost if this page resolves a previously mentioned exhibit
        for exhibit in exhibit_mentions:
            if exhibit in self.memory.exhibits:
                exhibit_data = self.memory.exhibits[exhibit]
                # This page contains the actual exhibit
                if not exhibit_data['resolved'] and page_id > min(exhibit_data['mentions']):
                    boosts['exhibit_resolution'] = 0.3
                    exhibit_data['resolved'] = True
                    exhibit_data['location_page'] = page_id
        
        # Boost if this page is referenced by earlier pages
        for ref in self.memory.references:
            if ref.target_type == 'page' and int(ref.target_id) == page_id:
                if ref.source_page < page_id:
                    boosts['referenced_by_earlier'] = 0.2
        
        # Boost if this page continues a concept thread
        for concept, pages in self.memory.concepts.items():
            if page_id in pages and len(pages) > 1:
                # This is part of an ongoing discussion
                boosts[f'concept_thread_{concept}'] = 0.15
        
        return boosts
    
    def get_page_context(self, page_id: int) -> Dict:
        """
        Get full context for a page including:
        - What pages reference this one
        - What exhibits are shown here
        - What concepts are discussed
        """
        # Find references TO this page
        incoming_refs = [
            ref for ref in self.memory.references
            if ref.target_type == 'page' and int(ref.target_id) == page_id
        ]
        
        # Find exhibits on this page
        page_exhibits = [
            ex_id for ex_id, ex_data in self.memory.exhibits.items()
            if ex_data.get('location_page') == page_id
        ]
        
        # Find concepts on this page
        page_concepts = [
            concept for concept, pages in self.memory.concepts.items()
            if page_id in pages
        ]
        
        return {
            'incoming_refs': incoming_refs,
            'exhibits_shown': page_exhibits,
            'concepts_discussed': page_concepts,
            'is_referenced': len(incoming_refs) > 0,
            'is_exhibit_location': len(page_exhibits) > 0
        }
    
    def get_rolling_context(self, page_id: int, window: int = 3) -> str:
        """
        Return concatenated text summaries from the preceding `window` pages.
        Used to give the scoring pipeline a human-readable narrative thread.
        """
        parts = []
        for i in range(max(0, page_id - window), page_id):
            summary = self.memory.page_summaries.get(i, "")
            if summary:
                parts.append(f"[p{i + 1}] {summary}")
        return " | ".join(parts)

    def get_rolling_context_score(self, page_id: int, window: int = 3) -> float:
        """
        Score [0, 1] reflecting how strongly prior pages set up the current page.

        Two signals are combined:
          1. Concept thread continuity — a concept that appeared in prior pages
             and also appears on this page contributes to narrative flow.
          2. Explicit backward references — prior pages that directly cite this
             page via "see page N" patterns.

        The score is deliberately modest (max 0.40 from concepts + 0.40 from
        refs) so it amplifies rather than overrides the primary pertinence signals.
        """
        if page_id == 0:
            return 0.0

        score = 0.0

        # Signal 1: concept thread continuation
        for concept, pages in self.memory.concepts.items():
            if page_id in pages:
                prior = [p for p in pages if p >= page_id - window and p < page_id]
                if prior:
                    score += 0.15
                    if len(prior) >= 2:
                        score += 0.05   # stronger thread = extra credit

        # Signal 2: incoming references from prior pages
        incoming = [
            ref for ref in self.memory.references
            if ref.target_type == 'page'
            and ref.target_id.isdigit()
            and int(ref.target_id) == page_id
            and ref.source_page < page_id
        ]
        if incoming:
            score += min(0.40, len(incoming) * 0.20)

        return round(min(1.0, score), 4)

    def get_memory_summary(self) -> Dict:
        """Get summary of all tracked memory for debugging."""
        return {
            'total_entities': len(self.memory.entities),
            'total_concepts': len(self.memory.concepts),
            'total_references': len(self.memory.references),
            'exhibits_tracked': len(self.memory.exhibits),
            'exhibits_resolved': sum(
                1 for ex in self.memory.exhibits.values() 
                if ex.get('resolved', False)
            ),
            'concept_distribution': {
                concept: len(pages) 
                for concept, pages in self.memory.concepts.items()
            }
        }
