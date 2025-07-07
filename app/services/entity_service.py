"""
Entity extraction service for parsing structured data from user messages.
"""

import json
import os
from typing import Dict, List

from ..services.openai_service import OpenAIService


class EntityService:
    """Entity extraction service using OpenAI function calling."""

    def __init__(self, openai_service=None):
        self.openai_service = openai_service or OpenAIService()

        # Required fields for API calls - RFQ-first workflow
        self.required_fields = {
            "buy_something": [
                "projectDesc",
                "description",
                "quantity",
                "unitofMeasures",
                "deliveryDate",
                "division",
                "location"
            ],
        }

    def extract_entities(self, message: str, context: dict = None, workflow_type: str = "buy_something") -> dict:
        """Extract entities using OpenAI function calling."""
        try:
            # Get schema from JSON file
            schema = self._get_schema(workflow_type)
            if not schema:
                return {"entities": {}, "completeness": 0, "confidence": 0}

            # Build prompt
            prompt = f"Extract entities from: '{message}'"
            if context and context.get("extracted_entities"):
                prompt += f"\nExisting entities: {context['extracted_entities']}"

            # Call OpenAI extract_entities method
            response = self.openai_service.extract_entities(
                message=prompt,
                workflow_type=workflow_type
            )

            # Merge with existing entities if present
            if context and context.get("extracted_entities"):
                existing = context["extracted_entities"]
                for key, value in existing.items():
                    if (
                        key not in response.get("entities", {})
                        or not response["entities"][key]
                    ):
                        response.setdefault("entities", {})[key] = value

            return response

        except Exception as e:
            print(f"Entity extraction error: {e}")
            return {"entities": {}, "completeness": 0, "confidence": 0}

    def check_entity_completeness(self, entities: dict, workflow_type: str) -> dict:
        """Check if required fields are present."""
        required = self.required_fields.get(workflow_type, [])
        entity_data = entities.get("entities", {}) if entities else {}

        missing = []
        present = 0

        for field in required:
            if entity_data and entity_data.get(field):
                present += 1
            else:
                missing.append(field)

        completeness = (present / len(required)) * 100 if required else 100

        return {
            "completeness": completeness,
            "is_complete": len(missing) == 0,
            "missing_required_fields": missing,
            "next_questions": self._get_questions_for_missing(missing, workflow_type),
        }

    def _get_schema(self, workflow_type: str) -> dict:
        """Load schema from JSON file."""

        schema_files = {
            "buy_something": "entity_extraction_rfq_creation.json",
        }

        filename = schema_files.get(workflow_type)
        if not filename:
            return {}

        schema_path = os.path.join(os.path.dirname(__file__), "..", "tools", filename)

        try:
            with open(schema_path, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"Schema file not found: {schema_path}")
            return {}

    def _get_questions_for_missing(
        self, missing_fields: List[str], workflow_type: str
    ) -> List[str]:
        """Generate questions for missing fields."""
        questions = []
        templates = {
            "description": "What product are you looking for?",
            "specification": "What are the technical specifications?",
            "category": "What category is this product?",
            "location": "Which city do you need this in?",
            "unitofMeasures": "What unit of measurement?",
            "projectDesc": "What is this RFQ for?",
            "quantity": "How many do you need?",
            "deliveryDate": "When do you need this delivered?",
            "division": "Which division does this belong to?",
        }

        for field in missing_fields:
            if field in templates:
                questions.append(templates[field])

        return questions
