from typing import Optional, Literal
from ..tools.confirmation_tool import ConfirmationTool

class ConfirmationService:
    def __init__(self, confirmation_tool: ConfirmationTool):
        self.confirmation_tool = confirmation_tool

    async def parse_confirmation(self, user_message: str) -> Optional[Literal["yes", "no"]]:
        return await self.confirmation_tool.parse_confirmation(user_message)