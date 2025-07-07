"""
Interaction logger for OpenAI API calls and responses.

Logs all OpenAI interactions to structured files for analysis,
training improvement, and monitoring purposes.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class InteractionLogger:
    """Logger for OpenAI interactions and responses."""
    
    def __init__(self, log_dir: str = "logs/openai_interactions"):
        """Initialize interaction logger with log directory."""
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        
        # Create daily log file
        today = datetime.now().strftime("%Y-%m-%d")
        self.log_file = self.log_dir / f"interactions_{today}.jsonl"
        
        logger.info(f"InteractionLogger initialized - Log file: {self.log_file}")
    
    def log_intent_classification(
        self,
        user_input: str,
        intent: str,
        confidence: float,
        reasoning: str,
        model_used: str,
        processing_time: float = None,
        all_scores: Dict[str, float] = None
    ):
        """Log intent classification interaction."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "interaction_type": "intent_classification",
            "user_input": user_input,
            "model_used": model_used,
            "response_data": {
                "intent": intent,
                "confidence": confidence,
                "reasoning": reasoning,
                "all_intent_scores": all_scores or {},
                "success": True
            },
            "processing_time_seconds": processing_time
        }
        
        self._write_log_entry(log_entry)
    
    def log_entity_extraction(
        self,
        user_input: str,
        entities: Dict[str, Any],
        completeness: float,
        workflow_type: str,
        model_used: str,
        processing_time: float = None,
        missing_fields: list = None
    ):
        """Log entity extraction interaction."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "interaction_type": "entity_extraction",
            "user_input": user_input,
            "model_used": model_used,
            "response_data": {
                "entities": entities,
                "completeness": completeness,
                "workflow_type": workflow_type,
                "missing_fields": missing_fields or [],
                "success": True
            },
            "processing_time_seconds": processing_time
        }
        
        self._write_log_entry(log_entry)
    
    def log_error(
        self,
        interaction_type: str,
        user_input: str,
        error_message: str,
        model_used: str = None
    ):
        """Log failed interaction."""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "interaction_type": interaction_type,
            "user_input": user_input,
            "model_used": model_used or "unknown",
            "response_data": {
                "error": error_message,
                "success": False
            }
        }
        
        self._write_log_entry(log_entry)
    
    def _write_log_entry(self, log_entry: Dict[str, Any]):
        """Write log entry to file."""
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
            
            logger.debug(f"Logged {log_entry['interaction_type']} interaction")
            
        except Exception as e:
            logger.error(f"Failed to write log entry: {e}")


# Global logger instance
_interaction_logger = None


def get_interaction_logger() -> InteractionLogger:
    """Get global interaction logger instance."""
    global _interaction_logger
    if _interaction_logger is None:
        _interaction_logger = InteractionLogger()
    return _interaction_logger