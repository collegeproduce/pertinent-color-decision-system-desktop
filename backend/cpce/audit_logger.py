"""
CPCE v5 - Logging & Audit Layer
Per specification section 13.
"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from pathlib import Path
import uuid

from .models import PageRepresentation, DecisionExplanation, Signal, SignalType


class AuditLogger:
    """
    Audit logging for all decisions.
    Every decision must be logged per specification section 13.
    """
    
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        
        # Set up structured logging
        self.logger = logging.getLogger("CPCE")
        self.logger.setLevel(logging.INFO)
        
        if not self.logger.handlers:
            handler = logging.FileHandler(self.log_dir / "cpce_audit.log")
            handler.setFormatter(
                logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            )
            self.logger.addHandler(handler)
    
    def log_decision(self, case_id: str, page_id: int, 
                     inputs: Dict[str, Any], 
                     intermediate_scores: Dict[str, float],
                     final_decision: bool,
                     reasoning_tree: DecisionExplanation,
                     processing_time_ms: float) -> str:
        """
        Log a decision per specification section 13.
        
        Required fields:
        - input signals
        - intermediate scores
        - final decision
        - reasoning tree
        """
        log_entry = {
            "audit_id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat(),
            "case_id": case_id,
            "page_id": page_id,
            "processing_time_ms": processing_time_ms,
            "inputs": {
                "signals": inputs.get("signals", {}),
                "visual_features": self._serialize_features(inputs.get("visual_features", {})),
                "semantic_features": self._serialize_features(inputs.get("semantic_features", {})),
                "page_role": inputs.get("page_role", "UNKNOWN"),
            },
            "intermediate_scores": intermediate_scores,
            "final_decision": final_decision,
            "reasoning_tree": reasoning_tree.to_dict() if reasoning_tree else {},
            "version": "5.0"
        }
        
        # Write to file
        self._write_log_entry(log_entry)
        
        # Also log to standard logger
        self.logger.info(f"Decision logged: case={case_id}, page={page_id}, decision={final_decision}")
        
        return log_entry["audit_id"]
    
    def _serialize_features(self, features: Any) -> Dict[str, Any]:
        """Convert feature objects to serializable dict."""
        if hasattr(features, '__dict__'):
            result = {}
            for key, value in features.__dict__.items():
                # Skip numpy arrays
                if hasattr(value, 'tolist'):
                    result[key] = value.tolist()
                else:
                    result[key] = value
            return result
        return dict(features) if features else {}
    
    def _write_log_entry(self, entry: Dict[str, Any]) -> None:
        """Write log entry to file."""
        case_id = entry["case_id"]
        log_file = self.log_dir / f"{case_id}.jsonl"
        
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
    
    def get_decision_history(self, case_id: str) -> List[Dict[str, Any]]:
        """Retrieve decision history for a case."""
        log_file = self.log_dir / f"{case_id}.jsonl"
        
        if not log_file.exists():
            return []
        
        history = []
        with open(log_file, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    history.append(json.loads(line))
        
        return history
    
    def generate_report(self, case_id: str) -> Dict[str, Any]:
        """Generate an audit report for a case."""
        history = self.get_decision_history(case_id)
        
        if not history:
            return {"error": "No history found for case"}
        
        total_pages = len(history)
        color_pages = sum(1 for h in history if h["final_decision"])
        overrides = sum(1 for h in history if h["reasoning_tree"].get("is_override", False))
        
        avg_confidence = sum(h["reasoning_tree"].get("confidence", 0) for h in history) / total_pages if total_pages > 0 else 0
        avg_processing_time = sum(h["processing_time_ms"] for h in history) / total_pages if total_pages > 0 else 0
        
        return {
            "case_id": case_id,
            "total_pages": total_pages,
            "color_pages": color_pages,
            "monochrome_pages": total_pages - color_pages,
            "overrides": overrides,
            "average_confidence": avg_confidence,
            "average_processing_time_ms": avg_processing_time,
            "decisions": [
                {
                    "page_id": h["page_id"],
                    "decision": h["final_decision"],
                    "confidence": h["reasoning_tree"].get("confidence", 0),
                    "processing_time_ms": h["processing_time_ms"]
                }
                for h in history
            ]
        }
    
    def log_error(self, case_id: str, page_id: int, error: str, context: Dict[str, Any] = None) -> None:
        """Log an error during processing."""
        log_entry = {
            "audit_id": str(uuid.uuid4()),
            "timestamp": datetime.utcnow().isoformat(),
            "case_id": case_id,
            "page_id": page_id,
            "error": error,
            "context": context or {},
            "type": "error"
        }
        
        self._write_log_entry(log_entry)
        self.logger.error(f"Error in case={case_id}, page={page_id}: {error}")
