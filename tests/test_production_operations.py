# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _compose() -> Mapping[str, Any]:
    return cast(
        Mapping[str, Any],
        yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8")),
    )


def test_every_published_compose_port_is_loopback_only() -> None:
    services = _compose()["services"]

    published = {
        service_name: str(port)
        for service_name, service in services.items()
        for port in service.get("ports", ())
    }

    assert published
    assert all(port.startswith("127.0.0.1:") for port in published.values())


def test_observability_has_no_default_admin_password_or_docker_socket() -> None:
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    services = _compose()["services"]
    grafana = services["grafana"]["environment"]
    minio = services["minio"]["environment"]

    assert grafana["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert grafana["GF_SECURITY_ADMIN_PASSWORD"] == "${LOCAL_AGENT_GRAFANA_ADMIN_PASSWORD:-}"
    assert minio["MINIO_ROOT_PASSWORD"] == "${LOCAL_AGENT_MINIO_ROOT_PASSWORD:-}"
    assert "/var/run/docker.sock" not in compose_text
    alloy = (ROOT / "observability/alloy/config.alloy").read_text(encoding="utf-8")
    assert "discovery.docker" not in alloy
    assert "loki.source.docker" not in alloy


def test_backup_and_restore_commands_are_syntax_valid_and_restore_is_disposable() -> None:
    scripts = (
        ROOT / "scripts/backup-coordination-postgres.sh",
        ROOT / "scripts/restore-coordination-postgres.sh",
        ROOT / "scripts/drill-coordination-restore.sh",
    )
    for script in scripts:
        subprocess.run(("bash", "-n", str(script)), check=True)

    restore = scripts[1].read_text(encoding="utf-8")
    assert "local_agent_restore_*" in restore
    assert "shasum -a 256 -c" in restore


def test_backup_launch_agent_matches_the_documented_rpo() -> None:
    path = ROOT / "scripts/launchd/com.rahul.local-first-agent.postgres-backup.plist"
    with path.open("rb") as handle:
        plist = plistlib.load(handle)

    assert plist["StartInterval"] == 21_600
    assert plist["RunAtLoad"] is True
    assert plist["ProgramArguments"] == ["__REPO_ROOT__/scripts/backup-coordination-postgres.sh"]


def test_recovery_and_refinery_residents_pin_their_authority() -> None:
    launchd = ROOT / "scripts/launchd"
    with (launchd / "com.rahul.local-first-agent.work-unit-crash-reconciler.plist").open(
        "rb"
    ) as handle:
        crash = plistlib.load(handle)
    with (launchd / "com.rahul.local-first-agent.refinery-fleet.plist").open("rb") as handle:
        refinery = plistlib.load(handle)

    assert crash["KeepAlive"] is True
    assert crash["ProgramArguments"][-4:] == [
        "--interval-seconds",
        "30",
        "--max-automatic-recoveries",
        "3",
    ]
    assert refinery["KeepAlive"] is True
    assert refinery["ProgramArguments"][-6:] == [
        "--target-project-id",
        "aidashos",
        "--target-project-id",
        "local-first-agent-os",
        "--interval-seconds",
        "10",
    ]


def test_bootstrap_inputs_and_ci_actions_are_immutable() -> None:
    pins = (ROOT / "scripts/toolchain-pins.env").read_text(encoding="utf-8")
    bootstrap = (ROOT / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    model_runtimes = (ROOT / "scripts/install-model-runtimes.sh").read_text(encoding="utf-8")
    frontier = (ROOT / "scripts/install-frontier-clis.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "UV_SHA256_DARWIN_ARM64=" in pins
    assert "UV_SHA256_LINUX_X86_64=" in pins
    assert 'git clone --branch "$NVM_VERSION"' in bootstrap
    assert 'git clone --branch "$LLAMA_CPP_REF"' in model_runtimes
    assert 'git clone --branch "$WHISPER_CPP_REF"' in model_runtimes
    assert '"@openai/codex@$CODEX_CLI_VERSION"' in frontier
    assert '"@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION"' in frontier
    for line in workflow.splitlines():
        if "uses:" in line:
            reference = line.split("@", 1)[1].split()[0]
            assert len(reference) == 40
            assert all(character in "0123456789abcdef" for character in reference)
