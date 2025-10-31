import logging
from typing import Optional
from difflib import SequenceMatcher
from app.services.openai_service import OpenAIService
from app.data import faq_config
FAQ_CONFIG = faq_config.FAQ_CONFIG
FULL_FAQ_CONTEXT = faq_config.FULL_FAQ_CONTEXT

logger = logging.getLogger(__name__)

class FAQService:
    def __init__(self):
        self.openai_service = OpenAIService()
        self.faq_config = FAQ_CONFIG

    def clean_text(self, text: str) -> str:
        """Clean text for WhatsApp: remove Markdown, bullets, and extra newlines"""
        replacements = {
            "*": "",
            "_": "",
            "#": "",
            "- ": "",
            "•": ""
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        return " ".join(text.split())  # normalize spaces

    def find_matching_question(self, user_question: str) -> Optional[str]:
        """Find an exact match for the user's question in FAQ_CONFIG"""
        user_q_clean = user_question.strip().lower().rstrip("?")

        for main_question, config in self.faq_config.items():
            if user_q_clean == main_question.strip().lower().rstrip("?"):
                return main_question

            for possible_q in config["possible_questions"]:
                if user_q_clean == possible_q.strip().lower().rstrip("?"):
                    return main_question

        return None
    async def get_faq_answer(self, user_question: str) -> str:
        """Get FAQ answer - first check predefined, then use LLM with full context"""

        # If not found, use LLM with full FAQ context
        prompt = f"""
Based on the following FAQ information, answer the user's question about GMT/Procucev Platform:

{FULL_FAQ_CONTEXT}

User Question: {user_question}

Instructions:
- Provide a direct, concise answer without greetings or salutations
- Use a professional but conversational tone suitable for WhatsApp
- If the answer is in the FAQ, provide it clearly and briefly
- If NOT in the FAQ, respond with: "I don't have specific information about that in our FAQ. Let me connect you with our support team for assistance."
- Do not add phrases like "Hello!", "What can I assist you with next?", or similar conversational fillers
- Keep the response focused and action-oriented
"""
        
        try:
            response = await self.openai_service.get_completion(prompt)
            return self.clean_text(response)
        except Exception as e:
            logger.error(f"error occurred in get_faq_answer function:{e}")
            return "I apologize, but I'm having trouble accessing the information right now. Please contact our support team for assistance."