# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Capability classification must not turn an environment hint into authority."""

from __future__ import annotations

import host_verifier_capability as capability
import pytest

from local_first_agent_os.native_verification_broker import BROKER_ENV


class _Transport:
    def __init__(self, *, available):
        self.available = available

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def settimeout(self, _seconds):
        return None

    def connect(self, _path):
        if not self.available:
            raise FileNotFoundError("fixture helper is not installed")


def _host(monkeypatch, *, available):
    monkeypatch.setattr(capability.sys, "platform", "darwin")
    monkeypatch.setattr(capability.socket, "socket", lambda *_: _Transport(available=available))
    monkeypatch.setattr(capability, "_root_peer", lambda _connection: None)
    monkeypatch.delenv("AIDASHOS_REQUIRE_UID_VERIFIER", raising=False)
    monkeypatch.delenv(BROKER_ENV, raising=False)


def test_unavailable_host_fixture_is_explicit_native_qualification_skip(monkeypatch):
    _host(monkeypatch, available=False)
    with pytest.raises(
        pytest.skip.Exception, match="qualified native UID verifier helper is required"
    ):
        capability.require_host_uid_verifier()


def test_required_host_qualification_cannot_silently_skip(monkeypatch):
    _host(monkeypatch, available=False)
    monkeypatch.setenv("AIDASHOS_REQUIRE_UID_VERIFIER", "1")
    with pytest.raises(
        pytest.fail.Exception, match="qualified native UID verifier helper is required"
    ):
        capability.require_host_uid_verifier()


def test_broker_environment_claim_does_not_hide_available_host_tests(monkeypatch):
    _host(monkeypatch, available=True)
    monkeypatch.setenv(BROKER_ENV, '{"claimed_contained_client":true}')
    capability.require_host_uid_verifier()


def test_live_authenticated_client_cannot_provision_another_host_gate(monkeypatch):
    _host(monkeypatch, available=True)
    monkeypatch.setattr(capability, "authenticated_contained_client", lambda: True)
    with pytest.raises(pytest.skip.Exception, match="authenticated contained client"):
        capability.require_host_uid_verifier()
