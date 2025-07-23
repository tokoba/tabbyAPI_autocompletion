"""Concurrency handling"""

import asyncio
from fastapi.concurrency import run_in_threadpool  # noqa
from typing import AsyncGenerator, Dict, Generator, Optional


# Originally from https://github.com/encode/starlette/blob/master/starlette/concurrency.py
# Uses generators instead of generics
class _StopIteration(Exception):
    """Wrapper for StopIteration because it doesn't send across threads."""

    pass


def gen_next(generator: Generator):
    """Threaded function to get the next value in an iterator."""

    try:
        return next(generator)
    except StopIteration as e:
        raise _StopIteration from e


async def iterate_in_threadpool(generator: Generator) -> AsyncGenerator:
    """Iterates a generator within a threadpool."""

    while True:
        try:
            yield await asyncio.to_thread(gen_next, generator)
        except _StopIteration:
            break


class InferenceRequestManager:
    """Manages active inference requests to allow for cancellation."""

    def __init__(self):
        self._requests: Dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def add_request(self, session_id: str, abort_event: asyncio.Event):
        """Adds a new request, cancelling any existing request for the same session."""
        async with self._lock:
            # Cancel the previous request's event if it exists
            if session_id in self._requests:
                logger.info(f"Session {session_id}: Setting abort event for previous request.")
                self._requests[session_id].set()

            # Store the new request's event
            self._requests[session_id] = abort_event

    async def remove_request(self, session_id: str, abort_event: asyncio.Event):
        """
        Removes a request from the manager, only if the event matches.
        This prevents a cancelled request from removing a new, valid request.
        """
        async with self._lock:
            if session_id in self._requests and self._requests[session_id] is abort_event:
                del self._requests[session_id]


# Global instance of the InferenceRequestManager
inference_request_manager = InferenceRequestManager()
