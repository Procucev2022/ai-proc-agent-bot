import logging
from app.services.openai_service import OpenAIService
from app.data import faq_config
FULL_FAQ_CONTEXT = faq_config.FULL_FAQ_CONTEXT

logger = logging.getLogger(__name__)

class FAQService:
    def __init__(self):
        self.openai_service = OpenAIService()

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
    async def get_faq_answer(self, user_question: str, conversation_history: dict = None) -> str:
        """Get FAQ answer - first check predefined, then use LLM with full context and conversation history"""

        # Extract last 5 user and bot messages from conversation history
        conversation_context = ""
        if conversation_history:
            messages = conversation_history.get('messages', [])
            # Get last 10 messages (5 user + 5 bot pairs)
            recent_messages = messages[-10:] if len(messages) > 10 else messages
            
            if recent_messages:
                conversation_context = "\n\nRecent conversation context:\n"
                for msg in recent_messages:
                    role = msg.get('role', 'unknown')
                    content = msg.get('content', '')
                    if role in ['user', 'assistant']:
                        role_label = 'User' if role == 'user' else 'Bot'
                        conversation_context += f"{role_label}: {content}\n"

            logger.info(f"conversation history for faq is :{conversation_context}")

        # If not found, use LLM with full FAQ context and conversation history
        prompt = f"""
Based on the following FAQ information, answer the user's question about GMT/Procucev Platform:

{FULL_FAQ_CONTEXT}
 

When generating the response, consider:
1. The user's current message: {user_question}
2. The relevant previous conversation context: {conversation_context}

Your final answer must combine both sources of information.
If the user’s question is related to the past conversation, use that context to give a complete answer.
If it is unrelated, answer only based on the current message.


Instructions:
- Provide a short, direct, concise answer without greetings or salutations
- Use a professional but conversational tone suitable for WhatsApp
- If the answer is in the FAQ, provide it clearly and briefly
- If NOT in the FAQ, respond with: "I don't have specific information about that in our FAQ. You can connect to our support team for assistance (info@procurev.com)."
- Do not add phrases like "Hello!", "What can I assist you with next?", or similar conversational fillers
- Keep the response focused and action-oriented
- Consider the conversation context when providing your answer

**Formatting Rules:**
- Start with an introductory sentence ending with "follow these steps"
- Use numbered list format: "1. ", "2. ", "3. ", "4. " (number, period, space)
- Each numbered item should be on its own line
- Add clarifications in parentheses within the step description
- End with a final summary sentence starting with "Remember,"
- Use semicolons (;) to separate multiple points within a single step
- Do NOT use bullet points, headers, or extra line breaks between steps
- Keep all text left-aligned with no indentation

Example format:
To [action] on WhatsApp via Procucev GMT, follow these steps
1. [First step with details] (clarification if needed). 
2. [Second step]; [additional detail within same step]. 

"""
        
        try:
            response = await self.openai_service.get_completion(prompt)
            return self.clean_text(response)
        except Exception as e:
            logger.error(f"error occurred in get_faq_answer function:{e}")
            return "I apologize, but I'm having trouble accessing the information right now. Please contact our support team for assistance."