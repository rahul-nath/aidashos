# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared test defaults must never use the checked-out source as writable state."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from local_first_agent_os import api
from local_first_agent_os.coordination import store
from local_first_agent_os.operator_identity import operator_token_file
from local_first_agent_os.settings import get_settings


def test_default_state_survives_a_child_working_directory_change(test_state_root: Path) -> None:
    checkout = test_state_root / "readonly-checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    checkout.chmod(0o555)
    try:
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from local_first_agent_os.coordination import store; "
                "from local_first_agent_os.settings import get_settings; "
                "import json; "
                "directory=store.coord_dir(); "
                "(directory/'child-state.txt').write_text('owned'); "
                "print(json.dumps([str(store.repo_root()), "
                "str(get_settings().coordination_root)]))",
            ],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    finally:
        checkout.chmod(0o755)
    assert json.loads(child.stdout) == [str(test_state_root)] * 2
    assert (test_state_root / ".agent_coordination/child-state.txt").read_text() == "owned"
    assert not (checkout / ".agent_coordination").exists()


def test_default_api_and_runtime_state_share_the_test_owner(
    test_state_root: Path, runtime, monkeypatch
) -> None:
    settings = get_settings()
    assert store.repo_root() == settings.coordination_root == test_state_root
    for path in (
        settings.config_dir,
        settings.projects_root,
        settings.artifact_root,
        settings.spool_dir,
        settings.session_context_export_dir,
        settings.saga_worktree_root,
        settings.lifecycle_log_dir,
        settings.lifecycle_maintenance_state_path,
        operator_token_file(),
        runtime.settings.coordination_root,
        runtime.settings.config_dir,
        runtime.settings.artifact_root,
    ):
        assert path.is_relative_to(test_state_root), path
    monkeypatch.setattr(api, "get_runtime", lambda: runtime)
    monkeypatch.setattr(api, "get_settings", lambda: runtime.settings)
    api.create_app()
    assert (test_state_root / "docs/gawd_drafts").is_dir()
