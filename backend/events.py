"""Lightweight SSE event bus — mqtt_subscriber pushes, app.py streams."""
import queue
import threading

_clients: list[queue.Queue] = []
_lock = threading.Lock()


def push(data: dict):
    """Broadcast data to every connected SSE client."""
    with _lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=20)
    with _lock:
        _clients.append(q)
    return q


def unsubscribe(q: queue.Queue):
    with _lock:
        if q in _clients:
            _clients.remove(q)
