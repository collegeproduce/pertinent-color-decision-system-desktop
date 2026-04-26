"""
CPCE v5 - Document Context Layer and Page Role Classifier
Per specification sections 1 and 2.
"""
import re
from typing import List, Dict, Tuple, Optional
from .models import DocumentContext, PageRepresentation, PageRole, CPCEConfig


class PageRoleClassifier:
    """
    Classifies the role of a page based on content and structure.
    Implements document context layer per specification.
    """
    
    # Role-specific keywords for classification
    EVIDENCE_KEYWORDS = [
        'exhibit', 'attachment', 'appendix', 'evidence',
        'photograph', 'image', 'figure', 'diagram',
        'chart', 'graph', 'table', 'medical record',
        'invoice', 'receipt', 'contract', 'agreement'
    ]
    
    NARRATIVE_KEYWORDS = [
        'introduction', 'background', 'summary',
        'argument', 'discussion', 'analysis',
        'conclusion', 'brief', 'memorandum'
    ]
    
    SIGNATURE_KEYWORDS = [
        'signature', 'signed', 'notary', 'affidavit',
        'verification', 'declaration', 'sworn'
    ]
    
    COVER_KEYWORDS = [
        'title', 'cover', 'page', 'table of contents'
    ]
    
    INDEX_KEYWORDS = [
        'index', 'table of contents', 'contents',
        'list of exhibits', 'list of attachments'
    ]
    
    HIGH_RISK_PATTERNS = [
        r'injury', r'wound', r'damage', r'medical',
        r'x-ray', r'financial\s+loss', r'damages',
        r'blood', r'bruise', r'fracture', r'trauma'
    ]
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
    
    def classify(self, page: PageRepresentation) -> PageRole:
        """
        Classify page role based on text content and visual features.
        """
        text = page.text.lower()
        
        # Check for explicit markers
        if self._has_any_keyword(text, self.COVER_KEYWORDS):
            if page.page_id == 0:
                return PageRole.COVER
        
        if self._has_any_keyword(text, self.INDEX_KEYWORDS):
            return PageRole.INDEX
        
        # Check for high-risk evidence
        if self._is_high_risk(text):
            return PageRole.EXHIBIT
        
        # Check for evidence markers
        evidence_score = self._score_keywords(text, self.EVIDENCE_KEYWORDS)
        narrative_score = self._score_keywords(text, self.NARRATIVE_KEYWORDS)
        signature_score = self._score_keywords(text, self.SIGNATURE_KEYWORDS)
        
        # Check for visual evidence (images, photos)
        if self._has_visual_evidence(page):
            evidence_score += 2.0
        
        # Determine role based on highest score
        scores = {
            PageRole.EVIDENCE: evidence_score,
            PageRole.NARRATIVE: narrative_score,
            PageRole.SIGNATURE: signature_score
        }
        
        best_role = max(scores, key=scores.get)
        
        # Only return role if score is meaningful
        if scores[best_role] > 0.5:
            return best_role
        
        return PageRole.UNKNOWN
    
    def _has_any_keyword(self, text: str, keywords: List[str]) -> bool:
        """Check if text contains any of the keywords."""
        return any(kw in text for kw in keywords)
    
    def _score_keywords(self, text: str, keywords: List[str]) -> float:
        """Score text based on keyword frequency."""
        score = 0.0
        for kw in keywords:
            count = text.count(kw)
            score += count * (1.0 if count == 1 else 0.7)
        return min(score, 5.0)  # Cap at 5
    
    def _is_high_risk(self, text: str) -> bool:
        """Check if page contains high-risk content per section 5."""
        for pattern in self.HIGH_RISK_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False
    
    def _has_visual_evidence(self, page: PageRepresentation) -> bool:
        """Check if page has visual evidence (photos, charts, etc.)."""
        text_lower = page.text.lower()
        visual_markers = [
            'photograph', 'photo', 'image', 'figure', 'chart',
            'diagram', 'exhibit', 'attachment'
        ]
        has_markers = any(m in text_lower for m in visual_markers)
        
        # Check visual features for image-like content
        vf = page.visual_features
        is_image_like = (
            vf.entropy > 4.0 and 
            vf.color_density > 0.1 and
            vf.spatial_distribution_score > 0.3
        )
        
        return has_markers or is_image_like


class DocumentContextLayer:
    """
    Manages document-level context and case isolation.
    Per specification section 7 (case isolation).
    """
    
    def __init__(self, config: CPCEConfig = None):
        self.config = config or CPCEConfig()
        self._current_context: Optional[DocumentContext] = None
        self._role_classifier = PageRoleClassifier(config)
    
    def initialize_case(self, case_id: str, document_path: str) -> DocumentContext:
        """
        Initialize a new case context per specification section 7.
        RESETS all memory, exhibit index, attention graph, weights, caches.
        """
        self._current_context = DocumentContext(
            case_id=case_id,
            document_type=self._detect_document_type(document_path),
            total_pages=0,
            pages=[],
            exhibit_index={},
            attention_graph={},
            high_risk_flags=[]
        )
        return self._current_context
    
    def _detect_document_type(self, path: str) -> str:
        """Detect document type from file path."""
        path_lower = path.lower()
        if 'brief' in path_lower:
            return 'brief'
        elif 'motion' in path_lower:
            return 'motion'
        elif 'contract' in path_lower:
            return 'contract'
        elif 'complaint' in path_lower:
            return 'complaint'
        elif 'exhibit' in path_lower:
            return 'exhibit_bundle'
        return 'unknown'
    
    def add_page(self, page: PageRepresentation) -> None:
        """Add a processed page to the context."""
        if self._current_context is None:
            raise RuntimeError("Context not initialized. Call initialize_case first.")
        
        # Classify page role
        page.role = self._role_classifier.classify(page)
        
        # Extract exhibit references
        exhibits = self._extract_exhibit_mentions(page.text)
        for ex in exhibits:
            self._current_context.exhibit_index[ex] = page.page_id
        
        # Check for high-risk content
        if self._role_classifier._is_high_risk(page.text):
            self._current_context.high_risk_flags.append(f"page_{page.page_id}")
        
        # Add to pages
        self._current_context.pages.append(page)
        self._current_context.total_pages = len(self._current_context.pages)
    
    def _extract_exhibit_mentions(self, text: str) -> List[str]:
        """Extract exhibit mentions from text."""
        import re
        pattern = r'[Ee]xhibit\s+([A-Z0-9-]+)'
        return re.findall(pattern, text)
    
    def get_context(self) -> Optional[DocumentContext]:
        """Get current document context."""
        return self._current_context
    
    def reset(self) -> None:
        """
        Reset engine for new case per specification section 16.
        """
        self._current_context = None
    
    def is_high_risk_page(self, page_id: int) -> bool:
        """
        Check if page is high-risk per specification section 5.
        """
        if self._current_context is None:
            return False
        
        # Check flags
        flag = f"page_{page_id}"
        if flag in self._current_context.high_risk_flags:
            return True
        
        # Check page role
        if page_id < len(self._current_context.pages):
            page = self._current_context.pages[page_id]
            return page.role == PageRole.EXHIBIT
        
        return False
    
    def build_attention_graph(self) -> Dict[int, List[int]]:
        """
        Build attention graph showing page relationships.
        """
        if self._current_context is None:
            return {}
        
        graph = {}
        pages = self._current_context.pages
        
        for i, page in enumerate(pages):
            neighbors = []
            
            # Previous and next pages are neighbors
            if i > 0:
                neighbors.append(i - 1)
            if i < len(pages) - 1:
                neighbors.append(i + 1)
            
            # Pages referenced in text are neighbors
            exhibits = self._extract_exhibit_mentions(page.text)
            for ex in exhibits:
                if ex in self._current_context.exhibit_index:
                    ref_page = self._current_context.exhibit_index[ex]
                    if ref_page != i and ref_page not in neighbors:
                        neighbors.append(ref_page)
            
            graph[i] = neighbors
        
        self._current_context.attention_graph = graph
        return graph
