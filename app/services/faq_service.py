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

    def find_matching_question(self, user_question: str, threshold: float = 0.6) -> Optional[str]:
        """Find matching main question from possible variations"""
        user_q_lower = user_question.lower()
        
        for main_question, config in self.faq_config.items():
            for possible_q in config["possible_questions"]:
                score = SequenceMatcher(None, user_q_lower, possible_q.lower()).ratio()
                if score >= threshold:
                    return main_question
        return None

    async def get_faq_answer(self, user_question: str) -> str:
        """Get FAQ answer - first check predefined, then use LLM with full context"""
        # First, try to find matching question
        main_question = self.find_matching_question(user_question)
        
        if main_question:
            logger.info(f"without using LLM:{main_question}")
            return self.faq_config[main_question]["answer"]
        
        # If not found, use LLM with full FAQ context
        prompt = f"""
Based on the following FAQ information, answer the user's question about GMT/Procucev:

{FULL_FAQ_CONTEXT}

User Question: {user_question}

Provide a helpful and accurate answer based on the FAQ information above. If the question is not covered in the FAQ, politely mention that and offer to connect them with support.
"""
        
        try:
            response = await self.openai_service.get_completion(prompt)
            return response
        except Exception as e:
            logger.error(f"error occurred in get_faq_answer function:{e}")
            return "I apologize, but I'm having trouble accessing the information right now. Please contact our support team for assistance."