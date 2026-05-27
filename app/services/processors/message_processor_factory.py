"""
Message Processor Factory.

Factory pattern for creating message processors based on message type.
"""

from typing import Dict, Any
from app.services.processors.text_message_processor import TextMessageProcessor
from app.services.processors.interactive_message_processor import InteractiveMessageProcessor
from app.services.processors.excel_message_processor import ExcelMessageProcessor
from app.services.processors.image_message_processor import ImageMessageProcessor


class MessageProcessorFactory:
    """Factory for creating message processors."""
    
    def __init__(self, dependencies: Dict[str, Any]):
        """Initialize with service dependencies."""
        self.dependencies = dependencies
        self._processors = {}
    
    def get_processor(self, message_type: str):
        """Get processor for message type."""
        if message_type not in self._processors:
            self._processors[message_type] = self._create_processor(message_type)
        
        return self._processors[message_type]
    
    def _create_processor(self, message_type: str):
        """Create processor for specific message type."""
        if message_type == "text":
            return TextMessageProcessor(**self.dependencies)
        elif message_type == "interactive":
            return InteractiveMessageProcessor(**self.dependencies)
        elif message_type == "excel_upload":
            return ExcelMessageProcessor(**self.dependencies)
        elif message_type in ["image", "document"]:
            return ImageMessageProcessor(**self.dependencies)
        else:
            raise ValueError(f"Unknown message type: {message_type}")