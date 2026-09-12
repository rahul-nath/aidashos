# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded cleanup for an already-owned agent process.

This module never creates process groups or changes the supervisor's ownership.
Callers retain one cleanup task covering their full resource scope, then wait for
it through cancellation before propagating the caller's cancellation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress

from .process_containment import ProcessContainmentUnavailable


async def await_cleanup[T](task: asyncio.Task[T]) -> T:
    """Delay even repeated caller cancellation until owned cleanup completes."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _consume_result[T](task: asyncio.Future[T]) -> None:
    if not task.cancelled():
        task.exception()


async def wait_bounded[T](awaitable: Awaitable[T], timeout: float) -> T:
    """Bound the caller's wait, including a slow awaitable's cancellation path."""
    pending = asyncio.ensure_future(awaitable)
    try:
        completed, _ = await asyncio.wait((pending,), timeout=timeout)
        if not completed:
            raise TimeoutError
        return pending.result()
    finally:
        if not pending.done():
            pending.cancel()
            pending.add_done_callback(_consume_result)


async def close_process(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 3,
    terminate_seconds: float = 2,
    kill_seconds: float = 2,
) -> None:
    """Close the input lifeline, then TERM/KILL and reap within finite waits."""
    if process.stdin is not None:
        process.stdin.close()
    try:
        await wait_bounded(process.wait(), grace_seconds)
        return
    except TimeoutError:
        pass
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await wait_bounded(process.wait(), terminate_seconds)
        return
    except TimeoutError:
        pass
    with suppress(ProcessLookupError):
        process.kill()
    try:
        await wait_bounded(process.wait(), kill_seconds)
    except TimeoutError as exc:
        raise ProcessContainmentUnavailable("agent process did not reap after SIGKILL") from exc
