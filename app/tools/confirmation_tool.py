import os
from typing import Optional, Literal
from ..services.openai_service import OpenAIService

class ConfirmationTool:
    def __init__(self, openai_service: OpenAIService):
        self.openai_service = openai_service
        self.response_keywords = {
            "yes": [
                'yes', 'y', 'yeah', 'yep', 'yup', 'ya', 'yas', 'yaa', 'yeh', 'ye', 'yess', 'yesss',
                'ok', 'okay', 'k', 'kk', 'okey', 'okie', 'oki', 'sure', 'confirm', 'confirmed',
                'correct', 'right', 'true', 'proceed', 'continue', 'go ahead', 'goahead',
                'accept', 'accepted', 'agree', 'approved', 'approve', '1', 'one', 'affirmative',
                'absolutely', 'definitely', 'ofcourse', 'of course', 'indeed', 'certainly',
                'fine', 'alright', 'all right', 'good', 'done', 'perfect', 'cool',
                '👍', '✓', '✔', '✅', 'हाँ', 'हां', 'ha', 'ji', 'ji ha', 'sahi', 'theek hai', 'thik hai',
                # Excel-specific confirmations
                'create rfqs', 'create rfq', 'make rfqs', 'make rfq', 'process', 'submit',
                'send rfqs', 'send rfq', 'forward', 'upload', 'create them', 'make them',
                # Button responses
                'confirm_registration', 'confirm_excel'
            ],
            "no": [
                'no', 'n', 'nah', 'nope', 'na', 'noo', 'nooo', 'nay', 'cancel', 'cancelled',
                'stop', 'restart', 'wrong', 'incorrect', 'false', 'deny', 'denied',
                'decline', 'declined', 'reject', 'rejected', 'disagree', 'not ok', 'not okay',
                'notok', 'nok', '0', 'zero', 'negative', 'abort', 'back', 'goback', 'go back',
                'reset', 'redo', 're-do', 'again', 'start over', 'startover', 'begin again',
                '2', 'two', '👎', '✗', '✘', '❌', 'नहीं', 'nahi', 'nhi', 'galat', 'wapas',
                # Excel-specific cancellations
                'clear', 'exit', 'reupload', 'upload again', 'new file', 'different file',
                'discard', 'delete', 'remove', 'start fresh', 'try again', 'redo excel',
                # Button responses
                'restart_registration', 'cancel_excel'
            ]
        }

    async def parse_confirmation(self, user_message: str) -> Optional[Literal["yes", "no"]]:
        message_lower = user_message.lower().strip()
        
        # Check exact matches first
        if message_lower in self.response_keywords["yes"]:
            return "yes"
        if message_lower in self.response_keywords["no"]:
            return "no"
        
        # Check partial matches for multi-word phrases
        for keyword in self.response_keywords["yes"]:
            if keyword in message_lower and len(keyword) > 2:  # Only check longer keywords
                return "yes"
        
        for keyword in self.response_keywords["no"]:
            if keyword in message_lower and len(keyword) > 2:  # Only check longer keywords
                return "no"
        
        # Fall back to AI parsing for ambiguous cases
        return await self._ai_parse_confirmation(user_message)

    async def _ai_parse_confirmation(self, user_message: str) -> Optional[Literal["yes", "no"]]:
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            response = await self.openai_service.parse_confirmation_response(user_message)
            if response in ["yes", "no"]:
                logger.info(f"AI successfully parsed confirmation: '{user_message}' -> '{response}'")
                return response
            else:
                logger.info(f"AI returned unclear result for: '{user_message}' -> '{response}'")
                return None
        except Exception as e:
            logger.error(f"Error in AI confirmation parsing: {e}")
            return None