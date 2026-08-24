# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The published schema is the source the web client is generated from.

Two committed artifacts stand between the Python models and the cockpit:
`web/openapi.json` and `web/src/api-types.ts`. A generated artifact that nobody
verifies drifts exactly like the hand-written mirror it replaced, so the checks
live here rather than only in a Makefile target an operator has to remember.

The schema dump runs as a subprocess for the same reason the script isolates its
own environment: describing the application must not depend on a developer's
`.env`, a live database, or the order pytest happened to import things in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_PATH = _REPO_ROOT / "web" / "openapi.json"
_TYPES_PATH = _REPO_ROOT / "web" / "src" / "api-types.ts"

# Every route whose response the generated client is expected to type. A route
# absent from this list is allowed to return an untyped body; a route on it is
# not, which is what stops a new endpoint from quietly publishing a bare object.
_TYPED_ROUTES = (
    "/projects/{project_id}/action",
    "/projects/{project_id}/activity",
    "/status-legend",
    "/work-units",
    "/work-units/{work_unit_id}",
    "/work-units/{work_unit_id}/next-commands",
    "/work-units/{work_unit_id}/events",
    "/work-units/{work_unit_id}/artifacts",
    "/work-units/{work_unit_id}/decisions",
    "/work-units/{work_unit_id}/cancel",
    "/work-units/{work_unit_id}/resume",
)


def test_the_committed_schema_matches_the_application() -> None:
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "dump_openapi.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )

    assert result.returncode == 0, (
        f"web/openapi.json is stale. Run `make api-types` and commit the result.\n"
        f"{result.stdout}{result.stderr}"
    )


def test_every_typed_route_declares_its_response_model() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

    for path in _TYPED_ROUTES:
        assert path in schema["paths"], f"{path} is missing from the published schema"
        for method, operation in schema["paths"][path].items():
            body = operation["responses"]["200"]["content"]["application/json"]["schema"]
            assert "$ref" in body, (
                f"{method.upper()} {path} publishes an untyped response body, so the "
                "generated client cannot type it. Declare response_model on the route."
            )


def test_the_next_commands_route_reuses_the_terminal_rule_table(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from work_unit_support import install_simulated_engine, run_acceptance_work_unit

    install_simulated_engine()
    work_unit_id = run_acceptance_work_unit()

    from local_first_agent_os import api
    from local_first_agent_os.work_units.next_commands import next_commands_for_view
    from local_first_agent_os.work_units.projection import build_work_unit_view

    view = build_work_unit_view(work_unit_id)
    expected = next_commands_for_view(view).model_dump(mode="json")
    monkeypatch.setattr(api.work_units, "get_work_unit", lambda _work_unit_id: view)

    response = TestClient(api.create_app()).get(f"/work-units/{work_unit_id}/next-commands")

    assert response.status_code == 200
    assert response.json() == expected


def test_the_generated_typescript_covers_every_published_schema() -> None:
    """The committed TypeScript must mention every schema the document declares.

    Cheaper than running the generator inside a Python test, and it catches the
    failure that actually happens: a model changed, the schema was regenerated,
    and the TypeScript was not.
    """

    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    generated = _TYPES_PATH.read_text(encoding="utf-8")

    missing = sorted(
        name for name in schema["components"]["schemas"] if f"{name}:" not in generated
    )
    assert missing == [], (
        f"web/src/api-types.ts is missing {missing}. Run `make api-types` and commit it."
    )


def test_the_work_unit_view_is_reachable_from_the_generated_client() -> None:
    """The one contract the WorkUnit cockpit is built on, pinned by name.

    Renaming `WorkUnitView` is allowed. Renaming it without regenerating is not,
    and this is the assertion that says so.
    """

    generated = _TYPES_PATH.read_text(encoding="utf-8")

    assert "WorkUnitView: {" in generated
    assert '"/work-units/{work_unit_id}"' in generated


@pytest.mark.parametrize("path", _TYPED_ROUTES)
def test_the_live_app_declares_the_same_response_models(
    path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running app agrees with the committed document.

    The document could be regenerated from a different revision than the one under
    test; this reads the routes directly, so the two statements have to match.
    """

    monkeypatch.setenv("LOCAL_AGENT_DATABASE_URL", "sqlite:///:memory:")
    from local_first_agent_os.settings import get_settings

    get_settings.cache_clear()
    from local_first_agent_os.api import create_app

    routes = {route.path: route for route in create_app().routes if isinstance(route, APIRoute)}
    assert path in routes, f"{path} is not a route on the application"
    assert routes[path].response_model is not None, (
        f"{path} declares no response_model, so its published shape is a bare object"
    )


def test_the_configuration_reference_matches_the_settings_model() -> None:
    """docs/configuration.md and .env.example are generated, not written.

    The hand-maintained version of this drifted badly: most environment
    variables the scripts read were absent from `.env.example`, and four of the
    ones present contradicted the model's defaults. Generation only helps if
    something fails when the committed copy goes stale.
    """

    completed = subprocess.run(
        [sys.executable, "scripts/dump_config_reference.py", "--check"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_both_generators_check_rather_than_write_when_invoked_bare() -> None:
    """The safe half is the default, and that is a property worth pinning.

    These scripts each own a committed artifact, so a bare invocation used to
    overwrite one. That is the wrong default for a file whose whole job is to
    fail when it drifts: a stray run rewrites the artifact to match the working
    tree and reports success, which is indistinguishable from there having been
    no drift at all. Verified by running each one bare against a clean tree and
    asserting it reports a match rather than a write.
    """

    for script in ("scripts/dump_openapi.py", "scripts/dump_config_reference.py"):
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        assert result.returncode == 0, f"{script} bare run failed:\n{result.stderr}"
        assert "wrote" not in result.stdout, (
            f"{script} wrote when invoked bare; checking must be the default"
        )
        assert "match" in result.stdout, (
            f"{script} bare run did not report a check:\n{result.stdout}"
        )
