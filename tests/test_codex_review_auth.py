# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic credential fixtures only; never open the operator's auth file."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from local_first_agent_os.capabilities import Capability
from local_first_agent_os.codex_review_client import (
    CodexSubscription,
    _ExternalSubscription,
    run_read_only_review,
)
from local_first_agent_os.codex_tool_worker import CodexToolWorker, InspectionRpcPolicy
from local_first_agent_os.process_containment import ProcessContainmentUnavailable
from local_first_agent_os.spawn_authority import SpawnAuthority


def _write_auth(
    path: Path, access: str = "fixture-access", account: str = "fixture-account"
) -> bytes:
    encoded = json.dumps(
        {
            "tokens": {
                "access_token": access,
                "account_id": account,
                "refresh_token": "REFRESH_MUST_STAY_WITH_OWNER",
                "id_token": "ID_TOKEN_NOT_REQUIRED",
            }
        }
    ).encode()
    path.write_bytes(encoded)
    return encoded


def test_external_login_cannot_rotate_or_copy_refresh_authority(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    before = _write_auth(auth)
    subscription = _ExternalSubscription(auth)
    assert subscription.login() == {
        "type": "chatgptAuthTokens",
        "accessToken": "fixture-access",
        "chatgptAccountId": "fixture-account",
    }
    assert auth.read_bytes() == before
    assert list(tmp_path.iterdir()) == [auth]


def test_refresh_consumes_only_a_new_access_token_from_the_same_owner(tmp_path: Path) -> None:
    auth = tmp_path / "auth.json"
    _write_auth(auth)
    subscription = _ExternalSubscription(auth)
    updated = _write_auth(auth, access="owner-refreshed-access")
    assert subscription.refresh(
        {"reason": "unauthorized", "previousAccountId": "fixture-account"}
    ) == {"accessToken": "owner-refreshed-access", "chatgptAccountId": "fixture-account"}
    assert auth.read_bytes() == updated


@pytest.mark.parametrize(
    ("access", "account", "previous"),
    [
        ("fixture-access", "fixture-account", "fixture-account"),
        ("new-access", "different-account", "fixture-account"),
        ("new-access", "fixture-account", "different-account"),
    ],
)
def test_refresh_refuses_stale_or_changed_identity(
    tmp_path: Path, access: str, account: str, previous: str
) -> None:
    auth = tmp_path / "auth.json"
    _write_auth(auth)
    subscription = _ExternalSubscription(auth)
    updated = _write_auth(auth, access, account)
    with pytest.raises(ProcessContainmentUnavailable, match="owning session"):
        subscription.refresh({"reason": "unauthorized", "previousAccountId": previous})
    assert auth.read_bytes() == updated


@pytest.mark.parametrize("encoded", [b"not-json", b"{}", b'{"tokens":null}', b'{"tokens":[]}'])
def test_invalid_auth_fails_without_echoing_credentials(tmp_path: Path, encoded: bytes) -> None:
    auth = tmp_path / "auth.json"
    auth.write_bytes(encoded)
    with pytest.raises(ProcessContainmentUnavailable, match="authenticate") as failure:
        _ExternalSubscription(auth)
    assert encoded.decode() not in str(failure.value)


@pytest.mark.parametrize("denial", ["INVOKE_MODEL", "admitted repository"])
def test_client_rejects_authority_or_repository_mismatch_before_auth(
    tmp_path: Path, denial: str
) -> None:
    grants = [Capability.READ_REPOSITORY]
    if denial == "admitted repository":
        grants.append(Capability.INVOKE_MODEL)
    other_repo = tmp_path / "other"
    other_repo.mkdir()
    worker = cast(
        CodexToolWorker,
        SimpleNamespace(
            policy=InspectionRpcPolicy(tmp_path, SpawnAuthority.of(grants)),
            boundary=SimpleNamespace(repository=tmp_path),
        ),
    )
    with pytest.raises(PermissionError, match=denial):
        asyncio.run(
            run_read_only_review(
                worker=worker,
                codex_bin="/must-not-run",
                repository=other_repo,
                model=CodexSubscription("fixture", tmp_path / "must-not-open"),
                prompt="fixture",
                emit=lambda _: None,
            )
        )
