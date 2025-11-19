# call_memory.py
from collections import deque
from typing import Optional, Dict
import asyncio

# Simple in-process queue of pending outbound calls
# Each entry: {"phone_number": "...", "customer_name": "..."}
_call_queue: deque[Dict[str, str]] = deque()
_lock = asyncio.Lock()


async def add_outbound_call(info: Dict[str, str]) -> None:
    """
    Add an outbound call context into the queue.
    Called from /start after we trigger Exotel.
    """
    async with _lock:
        _call_queue.append(info)


async def pop_next_outbound_call() -> Optional[Dict[str, str]]:
    """
    Pop the next outbound call context.
    Called from the WebSocket handler (bot) when Media Streams connects.
    """
    async with _lock:
        if _call_queue:
            return _call_queue.popleft()
        return None
