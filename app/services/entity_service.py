"""
Entity extraction service for parsing structured data from user messages.

This service uses LLM-powered entity extraction to parse user messages and
extract structured information like product types, specifications, locations,
and other relevant parameters needed for database queries and workflow processing.
It maintains entity completeness checks and handles iterative extraction loops.

Key responsibilities:
- Extract structured entities from unstructured user messages
- Parse product specifications, locations, quantities, and requirements
- Validate and normalize extracted entity values
- Maintain entity completeness for workflow requirements
- Handle iterative extraction when information is incomplete
- Support both BFS (product search) and RFQ workflow entity needs
"""

class EntityService:
    """
    Entity extraction service for parsing structured data from messages.
    
    Uses LLM to extract structured entities from unstructured user text
    for database queries and workflow processing.
    """
    
    def __init__(self):
        pass
        
    def extract_entities(self, message: str, context: dict, workflow_type: str):
        """
        Extract structured entities from user message for specific workflow.
        
        Uses LLM to parse unstructured text into structured entity dictionary
        required for database queries and workflow processing.
        """
        pass
        
    def check_entity_completeness(self, entities: dict, workflow_type: str):
        """
        Check if all required entities are present for workflow completion.
        
        Returns completeness status and list of missing required fields.
        """
        pass
        
    def _get_entity_schema(self, workflow_type: str):
        """Get entity schema for specific workflow type."""
        pass
        
    def _build_extraction_prompt(self, message: str, context: dict, schema: dict):
        """Build structured prompt for entity extraction."""
        pass
        
    def _validate_entities(self, entities: dict, schema: dict):
        """Validate and normalize extracted entities against schema."""
        pass
        
    def _merge_with_context(self, new_entities: dict, context: dict):
        """Merge new entities with existing context entities."""
        pass
        
    def _get_required_fields(self, workflow_type: str):
        """Get list of required fields for workflow completion."""
        pass