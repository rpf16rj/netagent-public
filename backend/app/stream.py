"""Broadcaster SSE — fan-out de snapshots/eventos para N subscribers.

Cada subscriber recebe um asyncio.Queue proprio; publish() faz put_nowait
(descarta para o cliente lento em vez de bloquear o poller).
"""
import asyncio
import json
from typing import Any, Dict, List


class Broadcaster:
    def __init__(self, max_queue: int = 50) -> None:
        self._subs: List[asyncio.Queue] = []
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subs.remove(q)
        except ValueError:
            pass

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, event: str, data: Dict[str, Any]) -> None:
        """Envia {event, data} a todos. Cliente com fila cheia perde a msg."""
        msg = {"event": event, "data": data}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # cliente lento: descarta, proximo ciclo recupera


def format_sse(msg: Dict[str, Any]) -> bytes:
    """Serializa uma mensagem no formato Server-Sent Events."""
    return ("event: %s\ndata: %s\n\n"
            % (msg["event"], json.dumps(msg["data"], default=str))).encode()
