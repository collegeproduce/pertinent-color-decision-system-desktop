"""
CPCE v5 - OCR Layer
Text extraction using Tesseract from local assets folder.
"""
import cv2
import numpy as np
import os
from pathlib import Path
from typing import List, Tuple, Dict, Any
from dataclasses import dataclass

# Set Tesseract path from assets folder (main.py directory) - MUST be done BEFORE importing pytesseract
BASE_DIR = Path(__file__).parent.parent  # cpce -> main directory
tesseract_path = BASE_DIR / "assets" / "Tesseract-OCR"
tessdata_path = tesseract_path / "tessdata"

if tesseract_path.exists():
    # Prepend to PATH so local tesseract is found first
    os.environ['PATH'] = str(tesseract_path) + os.pathsep + os.environ.get('PATH', '')
    
    # Set TESSDATA_PREFIX to tesseract root (parent of tessdata folder)
    # Tesseract looks for $TESSDATA_PREFIX/tessdata/eng.traineddata
    os.environ['TESSDATA_PREFIX'] = str(tesseract_path) + os.sep
    print(f"✅ TESSDATA_PREFIX set to: {os.environ['TESSDATA_PREFIX']}")
    
    # Set the command path for pytesseract
    tess_cmd = tesseract_path / "tesseract.exe"
    if tess_cmd.exists():
        print(f"✅ Tesseract found: {tess_cmd}")
    else:
        print(f"⚠️ Tesseract.exe not found at: {tess_cmd}")
else:
    print(f"⚠️ Tesseract folder not found at: {tesseract_path}")

# NOW import pytesseract after env vars are set
pytesseract = None
try:
    import pytesseract as pt
    # Explicitly set the tesseract command path
    if tesseract_path.exists() and (tesseract_path / "tesseract.exe").exists():
        pt.pytesseract.tesseract_cmd = str(tesseract_path / "tesseract.exe")
    pytesseract = pt
    print(f"✅ pytesseract imported successfully")
except ImportError as e:
    print(f"⚠️ pytesseract import failed: {e}")


@dataclass
class OCRResult:
    """Result of OCR processing."""
    text: str
    confidence: float
    words: List[Dict[str, Any]]
    regions: List[Dict[str, Any]]


class OCRLayer:
    """
    OCR processing layer using Tesseract from assets folder.
    """
    
    def __init__(self, lang: str = 'eng'):
        self.lang = lang
        self._available = pytesseract is not None
    
    def is_available(self) -> bool:
        """Check if OCR is available."""
        return self._available
    
    def extract_text(self, img: np.ndarray) -> OCRResult:
        """
        Extract text from image using Tesseract.
        Returns empty result if OCR fails or image has no text.
        """
        if not self._available or pytesseract is None:
            return OCRResult(text="", confidence=0.0, words=[], regions=[])
        
        try:
            # Set TESSDATA_PREFIX to tesseract root (folder containing tessdata subfolder)
            # Tesseract appends /tessdata/eng.traineddata to this path
            if tesseract_path.exists():
                os.environ['TESSDATA_PREFIX'] = str(tesseract_path) + os.sep
                print(f"  TESSDATA_PREFIX set: {os.environ['TESSDATA_PREFIX']}")
            
            # Preprocess image for better OCR
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            
            # Resize if too small
            h, w = gray.shape[:2]
            if h < 1000:
                scale = 1000 / h
                gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            
            # Denoise and threshold
            denoised = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)
            _, binary = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            
            # Build config with tessdata path - NO quotes to avoid path corruption
            tessdata_dir = str(tessdata_path) if tessdata_path.exists() else ""
            if tessdata_dir:
                config = f'--tessdata-dir {tessdata_dir} --psm 6'
            else:
                config = '--psm 6'
            
            # Run OCR
            text = pytesseract.image_to_string(binary, lang=self.lang, config=config)
            
            # Get confidence
            data = pytesseract.image_to_data(binary, lang=self.lang, config=config, output_type=pytesseract.Output.DICT)
            confidences = [int(c) for c in data['conf'] if int(c) > 0]
            avg_confidence = sum(confidences) / len(confidences) / 100.0 if confidences else 0.0
            
            return OCRResult(
                text=text.strip(),
                confidence=avg_confidence,
                words=[],
                regions=[]
            )
            
        except Exception as e:
            print(f"OCR failed: {e}")
            return OCRResult(text="", confidence=0.0, words=[], regions=[])
