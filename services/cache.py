import threading
import time


class TTLCache:
    def __init__(self, ttl: float = 60.0, max_items: int = 2048):
        self.ttl = ttl
        self.max_items = max_items
        self._d = {}
        self._l = threading.Lock()

    def get(self, key):
        with self._l:
            v = self._d.get(key)
            if v and time.time() - v[0] < self.ttl:
                return v[1]
            self._d.pop(key, None)
            return None

    def set(self, key, value):
        with self._l:
            now = time.time()
            self._d[key] = (now, value)
            # CLAUDE FIX: keys that are never read again were never removed (slow memory leak in a long run).
            if len(self._d) > self.max_items:
                for k in [k for k, v in self._d.items() if now - v[0] >= self.ttl]:
                    del self._d[k]
                while len(self._d) > self.max_items:
                    del self._d[min(self._d, key=lambda k: self._d[k][0])]
