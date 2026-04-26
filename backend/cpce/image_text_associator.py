"""
CPCE v5 - Image-Text Association Module
Links detected visual elements (charts, photos) with nearby text to establish context.
This is what makes the system "understand" that a chart is ABOUT injury damages.
"""
import cv2
import numpy as np
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field


@dataclass
class VisualElement:
    """A detected visual element with its bounding box and type."""
    element_type: str  # 'chart', 'photo', 'stamp', 'highlight'
    bbox: Tuple[int, int, int, int]  # (x, y, width, height)
    confidence: float
    region_id: int
    associated_text: str = ""
    context_keywords: List[str] = field(default_factory=list)
    importance_score: float = 0.5


@dataclass
class TextBlock:
    """A block of text with its position."""
    text: str
    bbox: Tuple[int, int, int, int]  # (x, y, width, height)
    confidence: float


class ImageTextAssociator:
    """
    Associates visual elements with nearby text to establish context.
    
    Key insight: A chart detected near text containing "injury" or "damages"
    is much more important than an isolated chart.
    """
    
    # Keywords that boost importance when near visuals
    HIGH_IMPORTANCE_KEYWORDS = [
        # Injury-related
        'injury', 'injuries', 'wound', 'fracture', 'bruise', 'scar', 'x-ray', 'mri', 'ct scan',
        'medical', 'hospital', 'surgery', 'treatment', 'pain', 'suffering',
        # Financial
        'damages', 'loss', 'compensation', 'award', 'settlement', 'expenses', 'costs',
        'medical bills', 'lost wages', 'future earnings',
        # Evidence
        'exhibit', 'evidence', 'proof', 'demonstrative', 'attachment',
        # Scene
        'accident', 'scene', 'collision', 'impact', 'vehicle', 'property damage'
    ]
    
    # Negation patterns that REDUCE importance
    NEGATION_PATTERNS = [
        'no evidence of', 'not related to', 'unrelated', 'no connection',
        'denies', 'deny', 'not responsible', 'no liability'
    ]
    
    def __init__(self, proximity_radius: int = 200):
        """
        Args:
            proximity_radius: Pixels within which text is considered "near" a visual element
        """
        self.proximity_radius = proximity_radius
    
    def associate(self, visual_elements: List[VisualElement], 
                  text_blocks: List[TextBlock],
                  image_shape: Tuple[int, int]) -> List[VisualElement]:
        """
        Associate visual elements with nearby text blocks.
        
        Returns:
            List of visual elements with populated associated_text and context_keywords
        """
        for element in visual_elements:
            # Find nearby text blocks
            nearby_texts = self._find_nearby_text(element, text_blocks)
            
            if nearby_texts:
                # Combine all nearby text
                combined_text = " ".join([t.text for t in nearby_texts])
                element.associated_text = combined_text
                
                # Extract context keywords
                element.context_keywords = self._extract_context_keywords(combined_text)
                
                # Calculate importance based on context
                element.importance_score = self._calculate_importance(
                    element.element_type, 
                    element.context_keywords,
                    combined_text
                )
            else:
                # Isolated visual - lower importance
                element.importance_score = self._base_importance(element.element_type) * 0.5
        
        return visual_elements
    
    def _find_nearby_text(self, element: VisualElement, 
                          text_blocks: List[TextBlock]) -> List[TextBlock]:
        """Find text blocks within proximity radius of the visual element."""
        nearby = []
        ex, ey, ew, eh = element.bbox
        element_center = (ex + ew/2, ey + eh/2)
        
        for text_block in text_blocks:
            tx, ty, tw, th = text_block.bbox
            text_center = (tx + tw/2, ty + th/2)
            
            # Calculate Euclidean distance
            distance = np.sqrt(
                (element_center[0] - text_center[0])**2 + 
                (element_center[1] - text_center[1])**2
            )
            
            if distance <= self.proximity_radius:
                nearby.append(text_block)
        
        return nearby
    
    def _extract_context_keywords(self, text: str) -> List[str]:
        """Extract high-importance keywords from text."""
        text_lower = text.lower()
        found_keywords = []
        
        for keyword in self.HIGH_IMPORTANCE_KEYWORDS:
            if keyword in text_lower:
                found_keywords.append(keyword)
        
        return found_keywords
    
    def _calculate_importance(self, element_type: str, 
                            keywords: List[str],
                            full_text: str) -> float:
        """
        Calculate importance score based on:
        - Element type (base importance)
        - Keyword presence (boost)
        - Negation patterns (penalty)
        """
        base_score = self._base_importance(element_type)
        
        # Keyword boost
        keyword_boost = len(keywords) * 0.15
        
        # Check for negation patterns
        negation_penalty = 0
        text_lower = full_text.lower()
        for negation in self.NEGATION_PATTERNS:
            if negation in text_lower:
                negation_penalty = 0.3  # Strong penalty
                break
        
        # Calculate final score
        final_score = base_score + keyword_boost - negation_penalty
        
        # Clamp to 0-1
        return max(0.0, min(1.0, final_score))
    
    def _base_importance(self, element_type: str) -> float:
        """Base importance score for different visual element types."""
        importance_map = {
            'injury_photo': 1.0,
            'scene_photo': 0.9,
            'medical_scan': 0.9,
            'financial_chart': 0.8,
            'chart': 0.6,
            'diagram': 0.5,
            'stamp': 0.7,
            'seal': 0.7,
            'highlight': 0.6,
            'photo': 0.7,
            'logo': 0.2,
            'decorative': 0.1
        }
        return importance_map.get(element_type, 0.5)
    
    def get_association_summary(self, elements: List[VisualElement]) -> Dict:
        """Get summary of all associations for logging/debugging."""
        summary = {
            'total_elements': len(elements),
            'with_context': sum(1 for e in elements if e.associated_text),
            'isolated': sum(1 for e in elements if not e.associated_text),
            'high_importance': sum(1 for e in elements if e.importance_score > 0.8),
            'avg_importance': np.mean([e.importance_score for e in elements]) if elements else 0
        }
        return summary
