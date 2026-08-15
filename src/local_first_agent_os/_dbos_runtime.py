# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DBOS singleton + decorator exports.

Lives in its own module so any repo file (repository, artifacts, model_manager)
can `from ._dbos_runtime import dbos_step` without pulling in `dbos_app`'s heavy
import graph. The DBOS singleton is constructed here at import time; afterwards
`DBOS.workflow` / `DBOS.step` are valid decorators. When `use_dbos` is off or
the dbos package is not importable, the decorators degrade to identity so the
rest of the app keeps working unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from .settings import Settings, get_settings

F = TypeVar("F", bound=Callable[..., Any])

if TYPE_CHECKING:
    from dbos import DBOSConfig
else:
    type DBOSConfig = dict[str, Any]


try:
    from dbos import DBOS, SetWorkflowID
except Exception:  # pragma: no cover - exercised only when dbos is unavailable
    DBOS = None  # type: ignore[assignment]
    SetWorkflowID = None  # type: ignore[assignment]


def _identity_decorator(*_args: Any, **_kwargs: Any) -> Callable[[F], F]:
    def wrap(func: F) -> F:
        return func

    return wrap


def _configured(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def build_dbos_runtime_config(settings: Settings) -> DBOSConfig:
    config: DBOSConfig = {
        "name": settings.app_name,
        "system_database_url": settings.dbos_system_database_url,
        "application_version": settings.application_version,
        "run_admin_server": settings.dbos_admin_server_enabled,
    }
    conductor_key = _configured(settings.dbos_conductor_key)
    conductor_url = _configured(settings.dbos_conductor_url)
    if conductor_key is not None:
        config["conductor_key"] = conductor_key
    if conductor_url is not None:
        config["conductor_url"] = conductor_url
    if conductor_key is not None:
        metadata = dict(settings.dbos_conductor_executor_metadata or {})
        metadata.setdefault("app", settings.app_name)
        metadata.setdefault("application_version", settings.application_version)
        config["conductor_executor_metadata"] = metadata
    executor_id = _configured(settings.dbos_executor_id)
    if executor_id is not None and conductor_key is None:
        config["executor_id"] = executor_id
    return config


_settings = get_settings()

if _settings.use_dbos and DBOS is not None:
    DBOS(config=build_dbos_runtime_config(_settings))
    dbos_workflow = DBOS.workflow
    dbos_step = DBOS.step
else:
    dbos_workflow = _identity_decorator
    dbos_step = _identity_decorator


__all__ = [
    "DBOS",
    "SetWorkflowID",
    "dbos_step",
    "dbos_workflow",
    "build_dbos_runtime_config",
]
