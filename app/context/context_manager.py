import threading

class ContextManager:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self._storage = {}

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            with cls._lock:
                if not cls._instance:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def set(self, request_id: str, key: str, value):
        self._storage.setdefault(request_id, {})[key] = value

    def get(self, request_id: str, key: str):
        return self._storage.get(request_id, {}).get(key)

    def update(self, request_id: str, key: str, updates: dict):
        """Only for dict/json values"""
        if request_id in self._storage and key in self._storage[request_id]:
            existing = self._storage[request_id][key]
            if isinstance(existing, dict):
                existing.update(updates)
                self._storage[request_id][key] = existing

    def delete(self, request_id: str, key: str):
        if request_id in self._storage:
            self._storage[request_id].pop(key, None)

    def clear(self, request_id: str):
        """Clear all context data for this request"""
        self._storage.pop(request_id, None)

# Global instance
context_manager = ContextManager()