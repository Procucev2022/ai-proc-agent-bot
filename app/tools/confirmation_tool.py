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
                '👍', '✓', '✔', '✅', 'हाँ', 'हां', 'ha', 'ji', 'ji ha', 'sahi', 'theek hai', 'thik hai'
            ],
            "no": [
                'no', 'n', 'nah', 'nope', 'na', 'noo', 'nooo', 'nay', 'cancel', 'cancelled',
                'stop', 'restart', 'wrong', 'incorrect', 'false', 'deny', 'denied',
                'decline', 'declined', 'reject', 'rejected', 'disagree', 'not ok', 'not okay',
                'notok', 'nok', '0', 'zero', 'negative', 'abort', 'back', 'goback', 'go back',
                'reset', 'redo', 're-do', 'again', 'start over', 'startover', 'begin again',
                '2', 'two', '👎', '✗', '✘', '❌', 'नहीं', 'nahi', 'nhi', 'galat', 'wapas'
            ]
        }

    async def parse_confirmation(self, user_message: str) -> Optional[Literal["yes", "no"]]:
        message_lower = user_message.lower().strip()
        
        if message_lower in self.response_keywords["yes"]:
            return "yes"
        if message_lower in self.response_keywords["no"]:
            return "no"
        
        return await self._ai_parse_confirmation(user_message)

    async def _ai_parse_confirmation(self, user_message: str) -> Optional[Literal["yes", "no"]]:
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            response = await self.openai_service.parse_confirmation_response(user_message)
            return response if response in ["yes", "no"] else None
        except Exception as e:
            logger.error(f"Error in AI confirmation parsing: {e}")