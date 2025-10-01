import logging
from .context_manager import context_manager

logger = logging.getLogger(__name__)

class SessionContext:
    KEY = "session"

    def set(self, request_id: str, data: dict):
        context_manager.set(request_id, self.KEY, data)

    def update(self, request_id: str, updates: dict):
        context_manager.update(request_id, self.KEY, updates)

    def get(self, request_id: str) -> dict:
        return context_manager.get(request_id, self.KEY)

    def delete(self, request_id: str):
        context_manager.delete(request_id, self.KEY)


class UserContext:
    KEY = "user"

    def set(self, request_id: str, data: dict):
        logger.info(f"Setting user context for request_id: {request_id}")
        context_manager.set(request_id, self.KEY, data)

    def update(self, request_id: str, updates: dict):
        context_manager.update(request_id, self.KEY, updates)

    def get(self, request_id: str) -> dict:
        result = context_manager.get(request_id, self.KEY)
        logger.info(f"Getting user context for request_id: {request_id}, found: {bool(result)}")
        return result

    def delete(self, request_id: str):
        context_manager.delete(request_id, self.KEY)


class ParamContext:
    KEY = "params"

    def set(self, request_id: str, value):
        context_manager.set(request_id, self.KEY, value)

    def get(self, request_id: str):
        return context_manager.get(request_id, self.KEY)

    def delete(self, request_id: str):
        context_manager.delete(request_id, self.KEY)


# Export singletons
session_context = SessionContext()
user_context = UserContext()
param_context = ParamContext()