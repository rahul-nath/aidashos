# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
from typing import cast

import pytest

from local_first_agent_os.agent_process_cleanup import await_cleanup, close_process, wait_bounded
from local_first_agent_os.process_containment import ProcessContainmentUnavailable


@pytest.mark.parametrize("finish_after", ["eof", "terminate", "kill", None])
def test_process_close_escalates_and_bounds_final_reap(finish_after):
    events = []

    class Stdin:
        def close(self):
            events.append("eof")

    class Process:
        stdin = Stdin()

        async def wait(self):
            if finish_after in events:
                return 0
            await asyncio.Future()

        def terminate(self):
            events.append("terminate")

        def kill(self):
            events.append("kill")

    async def exercise():
        closing = close_process(
            cast(asyncio.subprocess.Process, Process()),
            grace_seconds=0.01,
            terminate_seconds=0.01,
            kill_seconds=0.01,
        )
        if finish_after is None:
            with pytest.raises(ProcessContainmentUnavailable, match="did not reap"):
                await asyncio.wait_for(closing, 1)
        else:
            await asyncio.wait_for(closing, 1)

    asyncio.run(exercise())
    expected = ["eof", "terminate", "kill"]
    assert events == (
        expected if finish_after is None else expected[: expected.index(finish_after) + 1]
    )


def test_shared_cleanup_waits_through_repeated_cancellation():
    async def exercise():
        release = asyncio.Event()
        task = asyncio.create_task(release.wait())
        waiting = asyncio.create_task(await_cleanup(task))
        await asyncio.sleep(0)
        for _ in range(3):
            waiting.cancel()
            await asyncio.sleep(0)
        assert not waiting.done()
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert task.result() is True
        assert await await_cleanup(task) is True

    asyncio.run(exercise())


def test_cleanup_failure_is_not_hidden_by_caller_cancellation():
    async def exercise():
        release = asyncio.Event()

        async def cleanup():
            await release.wait()
            raise RuntimeError("cleanup failed")

        task = asyncio.create_task(cleanup())
        waiting = asyncio.create_task(await_cleanup(task))
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await waiting
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await await_cleanup(task)

    asyncio.run(exercise())


def test_timeout_does_not_wait_for_uncooperative_cancellation():
    async def exercise():
        release = asyncio.Event()

        async def slow_cancel():
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                await release.wait()

        pending = asyncio.create_task(slow_cancel())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(wait_bounded(pending, 0.01), 1)
            assert not pending.done()
        finally:
            release.set()
            await pending

    asyncio.run(exercise())
