"""Virtual monotonic time for bounded check polling and request pacing tests."""
import threading


class CheckClock:
    def __init__(self):
        self.value = 0.0
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.value

    def sleep(self, seconds):
        with self.lock:
            self.value += max(0, seconds)
