# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cheap operator-visible progress events for long local control-plane commands.

The durable ledger remains the source of truth.  These events are a best-effort
projection into the active Pi terminal stream so an operator can see each
dispatch phase and model turn without polling the database.

Every caller computes an operator sentence and this used to throw it away: the
log call passed the literal ``"dispatch_progress"`` and put only the aggregation
dimensions in ``extra``. So the eleven call sites produced eleven log lines whose
message was the same word, and the one sentence that said what happened survived
only in the in-process terminal event - which exists only when a Pi daemon
installed a sink, which is not the case for the resident dispatcher an operator
reads logs for.

The sentence now travels as ``detail``. The message stays ``dispatch_progress``
deliberately: it is the stable key everything aggregates on, and free-form model
text must never become a Loki label. ``detail`` and ``risks`` are body fields,
allowlisted in ``observability`` beside ``status``, which set that precedent.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Callable, Iterator, Sequence
from typing import Any

ProgressSink = Callable[[dict[str, Any]], None]

_sink: contextvars.ContextVar[ProgressSink | None] = contextvars.ContextVar(
    "local_agent_progress_sink",
    default=None,
)

logger = logging.getLogger(__name__)

# Names `logging.Logger.makeRecord` refuses in `extra`, because they would
# overwrite something the LogRecord already owns. It raises `KeyError` on them,
# which in this function's callers is a crash in the dispatch loop caused by
# naming a log field. The eleven current call sites happen not to collide;
# nothing stopped the twelfth.
_RESERVED_LOG_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


@contextlib.contextmanager
def progress_event_sink(sink: ProgressSink) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


class ReservedProgressField(ValueError):
    """A progress field named something a LogRecord already owns.

    A programmer error, so it raises rather than being renamed silently. The
    alternative - dropping it - would make a field that looks emitted and is not,
    which is the whole class of defect this module was fixed for.
    """


def emit_progress(
    message: str,
    *,
    phase: str,
    risks: Sequence[str] = (),
    **fields: Any,
) -> None:
    """Project one structured progress event to logs and the active terminal.

    ``message`` reaches the log as ``detail``. It is the sentence the caller
    computed, and the reason this function exists at all.

    ``risks`` is the machine-readable half: a failed task's own reasons, joined
    rather than nested because the log payload is flat. The live incident this
    fixes had ``401 Not logged in`` in a `PowWowTaskResult.risks` tuple that
    nothing forwarded, so the only copy was inside
    ``agent_execution_leases.result_json``, one indirection from anybody looking.
    """

    collisions = sorted(_RESERVED_LOG_FIELDS & set(fields))
    if collisions:
        raise ReservedProgressField(
            f"progress fields {collisions} would overwrite LogRecord attributes; rename them"
        )

    named = {key: value for key, value in fields.items() if value is not None}
    event = {
        "type": "status",
        "message": message,
        "phase": phase,
        **named,
    }
    if risks:
        event["risks"] = list(risks)
    # Keep operator text and aggregation dimensions in the same event.  The
    # formatter deliberately selects these stable keys; arbitrary model text is
    # never promoted into a Loki label, which is why `detail` rides in the body.
    logger.info(
        "dispatch_progress",
        extra={
            "phase": phase,
            "detail": message,
            **({"risks": "; ".join(risks)} if risks else {}),
            **named,
        },
    )
    sink = _sink.get()
    if sink is not None:
        sink(event)


__all__ = ["ReservedProgressField", "emit_progress", "progress_event_sink"]
