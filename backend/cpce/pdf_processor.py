"""
CPCE v5 - PDF Processing Utilities
Helper functions for loading and processing PDF files.
"""
import io
from typing import List, Tuple, Optional
from pathlib import Path
import numpy as np

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import cv2
except ImportError:
    cv2 = None
try:
    from PyQt6.QtGui import QImage
except ImportError:
    QImage = None

from .ocr_layer import OCRResult


class PDFProcessor:
    """Process PDF files for CPCE analysis."""
    
    def __init__(self, dpi: int = 150):
        self.dpi = dpi
        self._available = fitz is not None and cv2 is not None
    
    def is_available(self) -> bool:
        """Check if PDF processing is available."""
        return self._available
    
    def load_pdf(self, path: str) -> List[np.ndarray]:
        """
        Load PDF and convert pages to images.
        
        Returns:
            List of page images as numpy arrays (BGR format)
        """
        if not self._available:
            raise RuntimeError("PyMuPDF and OpenCV required for PDF processing")
        
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {path}")
        
        images = []
        
        try:
            doc = fitz.open(str(path))
            
            for page_num in range(len(doc)):
                page = doc[page_num]
                
                # Render page to image
                mat = fitz.Matrix(self.dpi/72, self.dpi/72)  # 72 is PDF default DPI
                pix = page.get_pixmap(matrix=mat)
                
                # Convert to numpy array
                img_data = np.frombuffer(pix.samples, dtype=np.uint8)
                
                # Simple reshape - let numpy handle the shape
                height, width = pix.height, pix.width
                
                if pix.n == 3:  # RGB
                    img = img_data.reshape(height, width, 3)
                elif pix.n == 4:  # RGBA
                    img = img_data.reshape(height, width, 4)
                else:  # Grayscale
                    img = img_data.reshape(height, width)
                
                # Convert to BGR for OpenCV
                if pix.n == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                elif pix.n == 4:
                    img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
                
                images.append(img)
            
            doc.close()
            
        except Exception as e:
            raise RuntimeError(f"Failed to process PDF: {e}")
        
        return images
    
    def get_page_count(self, path: str) -> int:
        """Get number of pages in PDF."""
        if not self._available:
            raise RuntimeError("PyMuPDF required")
        
        try:
            doc = fitz.open(str(path))
            count = len(doc)
            doc.close()
            return count
        except Exception as e:
            raise RuntimeError(f"Failed to get page count: {e}")
    
    def extract_text_native(self, path: str) -> List[str]:
        """Extract text from PDF using native PyMuPDF (no OCR)."""
        if not self._available:
            raise RuntimeError("PyMuPDF required")
        
        texts = []
        
        try:
            doc = fitz.open(str(path))
            
            for page in doc:
                text = page.get_text()
                texts.append(text)
            
            doc.close()
            
        except Exception as e:
            raise RuntimeError(f"Failed to extract text: {e}")
        
        return texts


def load_image(path: str) -> np.ndarray:
    """
    Load image from file.
    
    Returns:
        Image as numpy array (BGR format)
    """
    if cv2 is None:
        raise RuntimeError("OpenCV required")
    
    img = cv2.imread(str(path))
    if img is None:
        raise FileNotFoundError(f"Could not load image: {path}")
    
    return img
