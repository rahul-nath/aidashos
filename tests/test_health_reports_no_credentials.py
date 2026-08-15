# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`/health` must not publish the database password.

The endpoint returned `settings.database_url` whole, and every shipped
configuration carries credentials in that URL: the code default, `.env.example`,
`docker-compose.yml`, and `k8s/kind/app.yaml`. Anything that could reach the
port could read the password without presenting one.

Authenticating the endpoint is not the remedy and would be a regression. The
Compose healthcheck and both Kubernetes probes call `/health` with no
credential, so a gate there fails liveness and crashloops the pod. The endpoint
stays open and stops carrying the secret instead.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from local_first_agent_os.api import create_app
from local_first_agent_os.settings import DatabaseIdentity, Settings

_PASSWORD = "s3cret-not-in-any-response"
_CREDENTIAL_BEARING_URL = f"postgresql+psycopg://app_user:{_PASSWORD}@db.internal:6543/local_agent"


@pytest.fixture()
def served_app(runtime, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """The real app, pointed at a database whose URL carries a real password."""

    settings = runtime.settings.model_copy(update={"database_url": _CREDENTIAL_BEARING_URL})
    monkeypatch.setattr("local_first_agent_os.api.get_runtime", lambda: runtime)
    monkeypatch.setattr("local_first_agent_os.api.get_settings", lambda: settings)
    monkeypatch.setattr("local_first_agent_os.api.launch_dbos", lambda: None)
    return TestClient(create_app())


def test_health_body_contains_no_part_of_the_credential(served_app: TestClient) -> None:
    """The defect stated as behavior: the secret is absent from the raw bytes.

    Asserted against the response text rather than a parsed field, because a
    future edit could reintroduce the URL under any key and a field-by-field
    assertion would not notice.
    """

    with served_app as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert _PASSWORD not in response.text
    assert "app_user" not in response.text
    assert _CREDENTIAL_BEARING_URL not in response.text


def test_health_still_says_which_database_it_is_pointed_at(served_app: TestClient) -> None:
    """Removing the secret must not remove the diagnostic the endpoint is for."""

    with served_app as client:
        payload = client.get("/health").json()

    assert payload["database"] == {
        "backend": "postgresql+psycopg",
        "host": "db.internal",
        "port": 6543,
        "database": "local_agent",
    }


def test_health_needs_no_credential_of_its_own(served_app: TestClient) -> None:
    """The probes cannot present one, so the endpoint must not require one."""

    with served_app as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        pytest.param(
            "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/local_agent",
            DatabaseIdentity(
                backend="postgresql+psycopg", host="127.0.0.1", port=5432, database="local_agent"
            ),
            id="the shipped default, which carries credentials",
        ),
        pytest.param(
            "postgresql://db.internal:5432/local_agent",
            DatabaseIdentity(
                backend="postgresql", host="db.internal", port=5432, database="local_agent"
            ),
            id="no userinfo to drop",
        ),
        pytest.param(
            "postgresql://user@host/local_agent",
            DatabaseIdentity(backend="postgresql", host="host", port=None, database="local_agent"),
            id="a username and no password is still not reported",
        ),
        pytest.param(
            "sqlite:////data/app.sqlite3",
            DatabaseIdentity(backend="sqlite", host=None, port=None, database="/data/app.sqlite3"),
            id="an absolute sqlite path stays absolute",
        ),
        pytest.param(
            "sqlite:///app.sqlite3",
            DatabaseIdentity(backend="sqlite", host=None, port=None, database="app.sqlite3"),
            id="a relative sqlite path stays relative",
        ),
    ],
)
def test_identity_keeps_the_address_and_drops_the_credential(
    url: str, expected: DatabaseIdentity
) -> None:
    assert DatabaseIdentity.from_url(url) == expected


def test_a_malformed_port_does_not_break_the_endpoint() -> None:
    """A broken configuration must not become a crashlooping pod.

    `urlsplit().port` raises on a non-numeric port. Letting that reach the
    handler would 500 the liveness probe, and Kubernetes answers a failed
    liveness probe by killing the container - a far worse report of "your port
    is malformed" than a null.
    """

    identity = DatabaseIdentity.from_url("postgresql://user:pw@host:not-a-port/local_agent")

    assert identity.port is None
    assert identity.database == "local_agent"


def test_the_type_cannot_be_given_a_credential_field() -> None:
    """The guarantee is structural, not a redaction step someone must remember."""

    with pytest.raises(ValueError):
        DatabaseIdentity(backend="postgresql", host="h", password="oops")  # type: ignore[call-arg]


def test_settings_hands_out_the_identity_rather_than_the_url() -> None:
    """One place decides what is safe to report, so no caller has to."""

    settings = Settings.model_validate({"database_url": _CREDENTIAL_BEARING_URL})

    assert settings.database_identity.host == "db.internal"
    assert _PASSWORD not in settings.database_identity.model_dump_json()
