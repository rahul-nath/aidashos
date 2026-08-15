# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from .capability_gate import CallerIdentity, ensure_caller_may_use
from .chrome_devtools import (
    CHROME_CONTROL_RESULT_V1,
    ChromeControlFailure,
    ChromeControlService,
    ChromeDevToolsError,
    ChromeDevToolsErrorCode,
    ChromeLifecyclePhase,
)
from .contracts import WorkspaceId
from .ids import build_tool_call_id
from .policies import PolicyStore
from .repository import Repository
from .settings import Settings

WORKFLOWY_API_BASE_URL = "https://workflowy.com/api/v1"


class LocalTool(Protocol):
    name: str
    writes_external_state: bool

    def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ScriptTool:
    name: str
    command: list[str]
    timeout_seconds: int = 60
    writes_external_state: bool = False

    def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        proc = subprocess.run(
            self.command,
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"{self.name} exited {proc.returncode}")
        try:
            output = json.loads(proc.stdout)
        except json.JSONDecodeError:
            output = {"stdout": proc.stdout}
        return {"schema_version": "tool_output.v1", "tool_name": self.name, "output": output}


class WorkflowyApiClient:
    """Documented Workflowy v1 client. Caches the top-level listing for a TTL so
    repeated `workflowy_day_bullet_insert` calls do not blow the 1 req/min limit
    on `nodes-export`."""

    def __init__(self, settings: Settings, top_level_ttl_seconds: float = 30.0):
        self.settings = settings
        self.top_level_ttl_seconds = top_level_ttl_seconds
        self._top_level_cache: tuple[float, list[dict[str, Any]]] | None = None

    @property
    def api_key(self) -> str | None:
        return self.settings.workflowy_api_key

    @property
    def is_live(self) -> bool:
        return self.api_key is not None and not self.settings.workflowy_dry_run

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def list_top_level(self, force: bool = False) -> list[dict[str, Any]]:
        if not self.is_live:
            return []
        now = time.time()
        if (
            not force
            and self._top_level_cache is not None
            and now - self._top_level_cache[0] < self.top_level_ttl_seconds
        ):
            return list(self._top_level_cache[1])
        response = httpx.get(
            f"{WORKFLOWY_API_BASE_URL}/nodes",
            headers=self._headers(),
            params={"parent_id": "None"},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        nodes = list(body.get("nodes") or [])
        nodes.sort(key=lambda node: node.get("priority", 0))
        self._top_level_cache = (now, nodes)
        return list(nodes)

    def export_tree(self) -> dict[str, Any]:
        if not self.is_live:
            return {"nodes": []}
        response = httpx.get(
            f"{WORKFLOWY_API_BASE_URL}/nodes-export",
            headers=self._headers(),
            timeout=300,
        )
        response.raise_for_status()
        return response.json()

    def create_node(
        self,
        *,
        parent_id: str,
        name: str,
        position: str = "top",
        layout_mode: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"parent_id": parent_id, "name": name, "position": position}
        if layout_mode is not None:
            body["layoutMode"] = layout_mode
        if note is not None:
            body["note"] = note
        response = httpx.post(
            f"{WORKFLOWY_API_BASE_URL}/nodes",
            headers=self._headers(),
            json=body,
            timeout=60,
        )
        response.raise_for_status()
        self._top_level_cache = None
        return response.json()


class WorkflowyFetchTool:
    name = "workflowy_fetch_nodes"
    writes_external_state = False

    def __init__(self, settings: Settings, client: WorkflowyApiClient | None = None):
        self.settings = settings
        self.client = client or WorkflowyApiClient(settings)

    def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # TODO: move this check to a _pre_init_ function that blocks instantiation of the tool
        if self.settings.workflowy_fetch_script:
            return ScriptTool(
                name=self.name,
                command=[str(self.settings.workflowy_fetch_script)],
                timeout_seconds=300,
            ).run(workflow_id, payload)
        if not self.client.is_live:
            return {"schema_version": "workflowy_fetch.v1", "nodes": [], "dry_run": True}
        if payload.get("top_level_only"):
            return {
                "schema_version": "workflowy_fetch.v1",
                "nodes": self.client.list_top_level(force=bool(payload.get("force"))),
                "scope": "top_level",
            }
        return {
            "schema_version": "workflowy_fetch.v1",
            "nodes": self.client.export_tree().get("nodes", []),
            "scope": "export",
        }


class WorkflowyInsertTool:
    name = "workflowy_insert_node"
    writes_external_state = True

    def __init__(self, settings: Settings, client: WorkflowyApiClient | None = None):
        self.settings = settings
        self.client = client or WorkflowyApiClient(settings)

    def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        parent_node_id = str(payload["parent_node_id"])
        content = str(payload.get("content") or payload.get("name") or "")
        if self.settings.workflowy_insert_script:
            return ScriptTool(
                name=self.name,
                command=[str(self.settings.workflowy_insert_script)],
                timeout_seconds=30,
                writes_external_state=True,
            ).run(workflow_id, payload)
        if not self.client.is_live:
            return {
                "schema_version": "workflowy_insert_response.v1",
                "dry_run": True,
                "workflow_id": workflow_id,
                "parent_node_id": parent_node_id,
                "content_sha256": payload.get("content_sha256"),
                "name": content,
            }
        api_response = self.client.create_node(
            parent_id=parent_node_id,
            name=content,
            position=str(payload.get("position", "top")),
            layout_mode=payload.get("layoutMode"),
            note=payload.get("note"),
        )
        return {
            "schema_version": "workflowy_insert_response.v1",
            "dry_run": False,
            "workflow_id": workflow_id,
            "parent_node_id": parent_node_id,
            "content_sha256": payload.get("content_sha256"),
            "created_node_id": api_response.get("item_id"),
            "raw_response": api_response,
        }


class WorkflowyDayBulletTool:
    """Create a top-level MM/DD bullet next to /done and append a child to it.

    Always a new parent, never a reused one. An existing bullet for the same date
    is left alone: a day's captures are separate events, and folding them under
    one heading was a choice the outline should make, not this tool.

    ``month_day`` is optional and falls back to today. The fallback is resolved
    once, here, and the resolved value is returned in the response, because this
    runs inside a durable workflow: a replay that recomputed "today" would write
    to a different bullet than the original run. A caller that cares about the
    date across a retry should pass it, and `/send-to-wf` already does.

    Implementation strategy: use the documented top-level listing endpoint
    (`GET /api/v1/nodes?parent_id=None`) so we never trip the 1 req/min export
    limit, then post the MM/DD parent and child via `POST /api/v1/nodes` with
    `parent_id=None` for the parent and `parent_id=<MM/DD id>` for the child.
    """

    name = "workflowy_day_bullet_insert"
    writes_external_state = True

    def __init__(
        self,
        settings: Settings,
        fetch: WorkflowyFetchTool,
        insert: WorkflowyInsertTool,
        client: WorkflowyApiClient | None = None,
    ):
        self.settings = settings
        self.fetch = fetch
        self.insert = insert
        self.client = client or fetch.client

    def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        month_day = str(payload.get("month_day") or "").strip() or _today_month_day()
        content = str(payload["content"])
        content_sha256 = str(payload.get("content_sha256") or "")

        if not self.client.is_live:
            top_level: list[dict[str, Any]] = []
        else:
            top_level = self.client.list_top_level()
        done_node = _find_done_node(top_level)

        # The top-level listing is still read, but only to place the new bullet
        # next to /done. It is deliberately not searched for an existing MM/DD:
        # a second capture on the same date gets its own bullet.
        create_response = self.insert.run(
            workflow_id,
            {
                "parent_node_id": "None",
                "content": month_day,
                "name": month_day,
                "content_sha256": content_sha256 + ":parent",
                "position": "top",
            },
        )
        parent_node_id = str(create_response.get("created_node_id") or f"dryrun://{month_day}")
        created_parent = True

        insert_response = self.insert.run(
            workflow_id,
            {
                "parent_node_id": parent_node_id,
                "content": content,
                "content_sha256": content_sha256,
                "position": str(payload.get("position", "bottom")),
                "note": payload.get("note"),
            },
        )
        return {
            "schema_version": "workflowy_day_bullet_insert.v2",
            "month_day": month_day,
            "parent_node_id": parent_node_id,
            "parent_created": created_parent,
            "done_node_id": (
                str(done_node.get("id")) if done_node and done_node.get("id") else None
            ),
            "live": self.client.is_live,
            "insert": insert_response,
        }


def _today_month_day() -> str:
    """Today as MM/DD, in local time.

    Local rather than UTC because the bullet is a human's day, not a timestamp:
    a capture at 11pm belongs to the date the person writing it would name.
    """

    return datetime.now().strftime("%m/%d")


def _find_done_node(top_level: list[dict[str, Any]]) -> dict[str, Any] | None:
    for node in top_level:
        name = str(node.get("name") or "").strip().lower()
        if name in {"/done", "done"}:
            return node
    return None


def _find_top_level_match(
    top_level: list[dict[str, Any]],
    month_day: str,
    exclude: dict[str, Any] | None,
) -> dict[str, Any] | None:
    target = month_day.lstrip("0").replace("/0", "/")
    for node in top_level:
        if exclude is not None and node is exclude:
            continue
        name = str(node.get("name") or "").strip()
        normalized = name.lstrip("0").replace("/0", "/")
        if name == month_day or normalized == target:
            return node
    return None


class AppleNotesFetchTool:
    name = "apple_notes_fetch"
    writes_external_state = False

    def __init__(self, settings: Settings):
        self.settings = settings

    def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.settings.apple_notes_fetch_script:
            return ScriptTool(
                name=self.name,
                command=[str(self.settings.apple_notes_fetch_script)],
                timeout_seconds=300,
            ).run(workflow_id, payload)
        export_path = payload.get("export_path")
        if export_path and Path(export_path).exists():
            text = Path(export_path).read_text(encoding="utf-8", errors="replace")
            return {"schema_version": "apple_notes_snapshot.v1", "notes": [{"body": text}]}
        return {"schema_version": "apple_notes_snapshot.v1", "notes": [], "dry_run": True}


class ChromeDevToolsTool:
    name = "chrome_devtools"
    writes_external_state = True

    def __init__(
        self,
        settings: Settings,
        *,
        mutation_allowed: Callable[[], bool] | None = None,
    ):
        self.settings = settings
        self._mcp_service = ChromeControlService(
            settings,
            mutation_allowed=mutation_allowed or (lambda: True),
        )

    def run(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "list")
        args = [str(arg) for arg in payload.get("args") or []]
        if self.settings.chrome_devtools_transport != "mcp":
            return self._run_with_transport(workflow_id, payload)
        if action == "start":
            return self._mcp_service.start_action(workflow_id, args)
        if action == "status":
            return self._mcp_service.read_chrome_status(workflow_id, args)
        if action == "stop":
            return self._mcp_service.stop_action(workflow_id, args)
        started = time.monotonic()
        try:
            self._mcp_service.ensure_mutation_allowed(action)
            with self._mcp_service.action_lock:
                self._mcp_service.ensure_ready(explicit=False)
                legacy = self._run_with_transport(workflow_id, payload)
        except PermissionError as exc:
            error = ChromeDevToolsError(
                ChromeDevToolsErrorCode.MUTATION_NOT_ALLOWED,
                ChromeLifecyclePhase.READY,
                str(exc),
                status="blocked",
            )
            raise ChromeControlFailure(
                self._mcp_service.build_failure(workflow_id, action, started, error)
            ) from exc
        except ChromeDevToolsError as exc:
            raise ChromeControlFailure(
                self._mcp_service.build_failure(workflow_id, action, started, exc)
            ) from exc
        return self._mcp_service.build_success(
            workflow_id,
            action,
            started,
            extra=legacy,
        )

    def close(self) -> None:
        self._mcp_service.close()

    def _run_with_transport(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action") or "list")
        args = [str(arg) for arg in payload.get("args") or []]
        if action in {"gather", "summarize", "read", "decide"}:
            return self._run_gather(workflow_id, action, args)
        if action == "close_category":
            return self._run_close_category(workflow_id, args)
        invocations: list[dict[str, Any]] = []
        if action not in {"status", "start", "stop"} and self.settings.chrome_devtools_auto_start:
            invocations.extend(self._ensure_started())
        for command_args in self._commands_for(action, args):
            invocations.append(self._run_cli(command_args))
        return {
            "schema_version": CHROME_CONTROL_RESULT_V1,
            "workflow_id": workflow_id,
            "action": action,
            "args": args,
            "invocations": invocations,
        }

    def _run_gather(
        self,
        workflow_id: str,
        action: str,
        args: list[str],
    ) -> dict[str, Any]:
        category, options = self._parse_category_args(args)
        invocations: list[dict[str, Any]] = []
        if self.settings.chrome_devtools_auto_start:
            invocations.extend(self._ensure_started())
        list_result = self._run_cli(["list_pages"])
        invocations.append(list_result)
        pages = self._parse_pages_from_list_output(list_result)
        matched_pages = self._match_pages(pages, category)
        snapshots: list[dict[str, Any]] = []
        if action in {"summarize", "read", "decide"} and not options["no_snapshots"]:
            for page in matched_pages[: options["limit"]]:
                page_id = str(page["page_id"])
                select = self._run_cli(["select_page", page_id])
                snapshot = self._run_cli(["take_snapshot"])
                invocations.extend([select, snapshot])
                snapshots.append(
                    {
                        "page_id": page_id,
                        "title": page.get("title"),
                        "url": page.get("url"),
                        "snapshot": snapshot.get("stdout", ""),
                    }
                )
        screenshots: list[dict[str, Any]] = []
        if options["ocr"]:
            screenshot_dir = self.settings.spool_dir / "chrome_screenshots"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            for page in matched_pages[: options["limit"]]:
                page_id = str(page["page_id"])
                screenshot_path = screenshot_dir / (
                    f"{self._safe_file_stem(workflow_id)}_page_{page_id}.png"
                )
                select = self._run_cli(["select_page", page_id])
                screenshot = self._run_cli(
                    ["take_screenshot", "--filePath", str(screenshot_path), "--fullPage"]
                )
                invocations.extend([select, screenshot])
                screenshots.append(
                    {
                        "page_id": page_id,
                        "title": page.get("title"),
                        "url": page.get("url"),
                        "path": str(screenshot_path),
                        "result": screenshot.get("stdout", ""),
                    }
                )
        return {
            "schema_version": CHROME_CONTROL_RESULT_V1,
            "workflow_id": workflow_id,
            "action": action,
            "category": category,
            "decision_prompt": options["prompt"],
            "pages": pages,
            "matched_pages": matched_pages,
            "page_snapshots": snapshots,
            "page_screenshots": screenshots,
            "match_count": len(matched_pages),
            "snapshot_count": len(snapshots),
            "screenshot_count": len(screenshots),
            "invocations": invocations,
        }

    def _run_close_category(self, workflow_id: str, args: list[str]) -> dict[str, Any]:
        category, options = self._parse_category_args(args)
        confirmed = bool(options["confirmed"])
        if category in {"", "all", "*"} and not options["all"]:
            raise ValueError("/chrome close-category all requires --all and --yes.")
        invocations: list[dict[str, Any]] = []
        if self.settings.chrome_devtools_auto_start:
            invocations.extend(self._ensure_started())
        list_result = self._run_cli(["list_pages"])
        invocations.append(list_result)
        pages = self._parse_pages_from_list_output(list_result)
        matched_pages = self._match_pages(pages, category)
        closed_page_ids: list[str] = []
        if confirmed:
            for page in matched_pages:
                page_id = str(page["page_id"])
                close_result = self._run_cli(["close_page", page_id], allow_failure=True)
                invocations.append(close_result)
                if close_result["returncode"] == 0:
                    closed_page_ids.append(page_id)
        return {
            "schema_version": CHROME_CONTROL_RESULT_V1,
            "workflow_id": workflow_id,
            "action": "close_category",
            "category": category,
            "confirmed": confirmed,
            "dry_run": not confirmed,
            "pages": pages,
            "matched_pages": matched_pages,
            "match_count": len(matched_pages),
            "closed_page_ids": closed_page_ids,
            "invocations": invocations,
            "warning": None
            if confirmed
            else "Dry run only. Re-run with --yes to close the matched tabs.",
        }

    def _commands_for(self, action: str, args: list[str]) -> list[list[str]]:
        if action == "status":
            return [["status"]]
        if action == "start":
            return [["start", *(args or self.settings.chrome_devtools_start_args)]]
        if action == "stop":
            return [["stop"]]
        if action == "list":
            return [["list_pages"]]
        if action == "open":
            url, rest = self._pop_required(args, "/chrome open <url>")
            return [["new_page", url, *rest]]
        if action == "select":
            page_id, rest = self._pop_required(args, "/chrome select <page_id>")
            self._validate_page_id(page_id)
            return [["select_page", page_id, "--bringToFront", *rest]]
        if action == "close":
            page_id, rest = self._pop_required(args, "/chrome close <page_id>")
            if rest:
                raise ValueError("/chrome close accepts only a page id.")
            self._validate_page_id(page_id)
            return [["close_page", page_id]]
        if action == "navigate":
            page_id, rest = self._extract_page_id(args)
            url, flags = self._pop_required(rest, "/chrome navigate [page_id] <url>")
            return self._with_optional_select(
                page_id,
                ["navigate_page", "--type", "url", "--url", url, *flags],
            )
        if action in {"back", "forward", "reload"}:
            page_id, rest = self._extract_page_id(args)
            return self._with_optional_select(
                page_id,
                ["navigate_page", "--type", action, *rest],
            )
        if action == "evaluate":
            page_id, rest = self._extract_page_id(args)
            function = " ".join(rest).strip()
            if not function:
                raise ValueError(
                    '/chrome eval requires a JavaScript function, e.g. "() => document.title".'
                )
            return self._with_optional_select(page_id, ["evaluate_script", function])
        if action == "screenshot":
            page_id, rest = self._extract_page_id(args)
            command = ["take_screenshot"]
            if rest and not rest[0].startswith("--"):
                command.extend(["--filePath", rest.pop(0)])
            command.extend(rest)
            return self._with_optional_select(page_id, command)
        if action == "snapshot":
            page_id, rest = self._extract_page_id(args)
            command = ["take_snapshot"]
            if rest and not rest[0].startswith("--"):
                command.extend(["--filePath", rest.pop(0)])
            command.extend(rest)
            return self._with_optional_select(page_id, command)
        if action == "console":
            page_id, rest = self._extract_page_id(args)
            return self._with_optional_select(page_id, ["list_console_messages", *rest])
        if action == "network":
            page_id, rest = self._extract_page_id(args)
            return self._with_optional_select(page_id, ["list_network_requests", *rest])
        raise ValueError(f"Unsupported chrome action: {action}")

    def _ensure_started(self) -> list[dict[str, Any]]:
        if self.settings.chrome_devtools_transport == "mcp":
            return []
        status = self._run_cli(["status"], allow_failure=True)
        status_text = f"{status.get('stdout', '')}\n{status.get('stderr', '')}".lower()
        is_running = "running" in status_text and "not running" not in status_text
        configured_args = [
            arg.lower()
            for arg in [
                *self._legacy_attach_args(),
                *self.settings.chrome_devtools_start_args,
            ]
        ]
        has_configured_args = all(arg in status_text for arg in configured_args)
        if status["returncode"] == 0 and is_running and has_configured_args:
            return [status]
        start = self._run_cli(
            [
                "start",
                *self._legacy_attach_args(),
                *self.settings.chrome_devtools_start_args,
            ]
        )
        return [status, start]

    def _legacy_attach_args(self) -> list[str]:
        mode = self.settings.chrome_devtools_attach_mode
        if mode == "auto_connect":
            return ["--auto-connect"]
        if mode == "browser_url" and self.settings.chrome_devtools_browser_url:
            return ["--browser-url", self.settings.chrome_devtools_browser_url]
        if mode == "launch":
            return list(self.settings.chrome_devtools_launch_args)
        return []

    def _run_cli(
        self,
        command_args: list[str],
        *,
        allow_failure: bool = False,
    ) -> dict[str, Any]:
        if self.settings.chrome_devtools_transport == "mcp":
            return self._mcp_service.call_tool_command(command_args, allow_failure=allow_failure)
        command = [
            self.settings.chrome_devtools_command,
            *self.settings.chrome_devtools_command_args,
            *command_args,
        ]
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=self.settings.chrome_devtools_timeout_seconds,
            check=False,
        )
        output: dict[str, Any] = {
            "command": command,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        with suppress(json.JSONDecodeError):
            output["json"] = json.loads(proc.stdout)
        if proc.returncode != 0 and not allow_failure:
            message = (
                proc.stderr.strip()
                or proc.stdout.strip()
                or f"Chrome DevTools CLI exited {proc.returncode}"
            )
            raise RuntimeError(message)
        return output

    def _parse_category_args(self, args: list[str]) -> tuple[str, dict[str, Any]]:
        tokens = list(args)
        category_parts: list[str] = []
        options: dict[str, Any] = {
            "all": False,
            "confirmed": False,
            "limit": 5,
            "no_snapshots": False,
            "ocr": False,
            "prompt": None,
        }
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token in {"--all", "-a"}:
                options["all"] = True
            elif token in {"--yes", "--confirm", "--force"}:
                options["confirmed"] = True
            elif token == "--no-snapshots":
                options["no_snapshots"] = True
            elif token == "--ocr":
                options["ocr"] = True
            elif token in {"--prompt", "--question", "--instruction", "--"}:
                options["prompt"] = " ".join(tokens[index + 1 :]).strip() or None
                break
            elif token == "--limit":
                try:
                    options["limit"] = max(1, int(tokens[index + 1]))
                except (IndexError, ValueError) as exc:
                    raise ValueError("--limit requires a positive integer.") from exc
                index += 1
            else:
                category_parts.append(token)
            index += 1
        category = " ".join(category_parts).strip().lower()
        if options["all"] and not category:
            category = "all"
        return category, options

    def _parse_pages_from_list_output(self, invocation: dict[str, Any]) -> list[dict[str, Any]]:
        parsed = invocation.get("json")
        if isinstance(parsed, list):
            return [self._normalize_page(item, str(index + 1)) for index, item in enumerate(parsed)]
        if isinstance(parsed, dict):
            candidates = parsed.get("pages") or parsed.get("data")
            if isinstance(candidates, list):
                return [
                    self._normalize_page(item, str(index + 1))
                    for index, item in enumerate(candidates)
                ]
        pages: list[dict[str, Any]] = []
        for raw_line in str(invocation.get("stdout", "")).splitlines():
            line = raw_line.strip()
            match = re.match(r"^(?P<page_id>\d+):\s*(?P<label>.+?)(?:\s+\[selected\])?$", line)
            if not match:
                continue
            label = match.group("label").strip()
            selected = "[selected]" in line
            url_match = re.search(r"((?:https?://|chrome://|about:)\S+)$", label)
            url = url_match.group(1) if url_match else None
            title = label[: url_match.start()].strip() if url_match else label
            pages.append(
                self._normalize_page(
                    {
                        "id": match.group("page_id"),
                        "title": title or url or label,
                        "url": url,
                        "selected": selected,
                    },
                    match.group("page_id"),
                )
            )
        return pages

    def _normalize_page(self, page: Any, fallback_id: str) -> dict[str, Any]:
        if not isinstance(page, dict):
            label = str(page)
            return {
                "page_id": fallback_id,
                "title": label,
                "url": label if self._looks_like_url(label) else None,
                "label": label,
                "selected": False,
            }
        page_id = str(page.get("page_id") or page.get("id") or page.get("index") or fallback_id)
        title = str(page.get("title") or page.get("name") or page.get("label") or "")
        url = str(page.get("url") or page.get("href") or "").strip() or None
        label = str(page.get("label") or " ".join(part for part in [title, url or ""] if part))
        return {
            "page_id": page_id,
            "title": title or label,
            "url": url,
            "label": label,
            "selected": bool(page.get("selected")),
        }

    def _match_pages(self, pages: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
        normalized_category = category.strip().lower()
        if normalized_category in {"", "all", "*"}:
            return [dict(page, match_score=1.0) for page in pages]
        if normalized_category.isdigit():
            return [
                dict(page, match_score=1.0)
                for page in pages
                if str(page.get("page_id")) == normalized_category
            ]
        tokens = [
            token
            for token in re.split(r"[^a-z0-9]+", normalized_category)
            if token and token not in {"tab", "tabs", "page", "pages"}
        ]
        if not tokens:
            return []
        matches: list[dict[str, Any]] = []
        for page in pages:
            haystack = " ".join(
                str(page.get(key) or "").lower() for key in ("title", "url", "label")
            )
            score = sum(1 for token in tokens if token in haystack)
            if score == len(tokens):
                matches.append(dict(page, match_score=score / len(tokens)))
        return matches

    def _looks_like_url(self, value: str) -> bool:
        return value.startswith(("http://", "https://", "chrome://", "about:"))

    def _safe_file_stem(self, value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_")[:120] or "chrome"

    def _with_optional_select(self, page_id: str | None, command: list[str]) -> list[list[str]]:
        if page_id is None:
            return [command]
        self._validate_page_id(page_id)
        return [["select_page", page_id, "--bringToFront"], command]

    def _extract_page_id(self, args: list[str]) -> tuple[str | None, list[str]]:
        rest = list(args)
        if not rest:
            return None, rest
        if rest[0].isdigit():
            return rest.pop(0), rest
        if "--page" in rest:
            index = rest.index("--page")
            try:
                page_id = rest[index + 1]
            except IndexError as exc:
                raise ValueError("--page requires a numeric page id.") from exc
            del rest[index : index + 2]
            return page_id, rest
        return None, rest

    def _pop_required(self, args: list[str], usage: str) -> tuple[str, list[str]]:
        if not args:
            raise ValueError(f"{usage} is required.")
        return args[0], list(args[1:])

    def _validate_page_id(self, page_id: str) -> None:
        if not page_id.isdigit():
            raise ValueError(f"Chrome page id must be numeric; got {page_id}.")


class ToolRegistry:
    def __init__(self, settings: Settings, policy_store: PolicyStore, repository: Repository):
        self.settings = settings
        self.policy_store = policy_store
        self.repository = repository
        client = WorkflowyApiClient(settings)
        fetch = WorkflowyFetchTool(settings, client)
        insert = WorkflowyInsertTool(settings, client)
        self.tools: dict[str, LocalTool] = {
            "workflowy_fetch_nodes": fetch,
            "workflowy_insert_node": insert,
            "workflowy_day_bullet_insert": WorkflowyDayBulletTool(
                settings, fetch, insert, client=client
            ),
            "apple_notes_fetch": AppleNotesFetchTool(settings),
            "chrome_devtools": ChromeDevToolsTool(
                settings,
                mutation_allowed=lambda: (
                    self.policy_store.get(WorkspaceId.CHROME.value).write_enabled
                ),
            ),
        }
        self.workflowy_client = client

    def close(self) -> None:
        """Release long-lived local tool resources owned by this registry."""

        for tool in self.tools.values():
            close = getattr(tool, "close", None)
            if callable(close):
                close()

    def run(
        self,
        *,
        workflow_id: str,
        workspace_id: str,
        tool_name: str,
        payload: dict[str, Any],
        caller: CallerIdentity,
        pi_turn_id: str | None = None,
        enforce_policy: bool = True,
    ) -> dict[str, Any]:
        """Dispatch one tool, after asking both questions about it.

        `workspace_id` answers where the call is happening and `caller` answers
        who is making it. They are separate because they constrain different
        things: a workspace bounds paths and tools by context, an identity is
        checked against grants. Answering only the first is what let the approved
        parent gate live on one route while every other path to the same tool
        went unchecked.

        `caller` is required and has no default. A default would be a principal
        nobody chose, and the whole point is that the call site says who is
        acting.
        """

        ensure_caller_may_use(caller, tool_name)
        if enforce_policy:
            self.policy_store.ensure_tool_allowed(workspace_id, tool_name)
        tool = self.tools[tool_name]
        tool_call_id = build_tool_call_id(workflow_id, tool_name, payload)
        try:
            output = tool.run(workflow_id, payload)
            self.repository.record_tool_call(
                tool_call_id=tool_call_id,
                workflow_id=workflow_id,
                tool_name=tool_name,
                input_json=payload,
                output_json=output,
                status="completed",
                pi_turn_id=pi_turn_id,
            )
            return output
        except ChromeControlFailure as exc:
            self.repository.record_tool_call(
                tool_call_id=tool_call_id,
                workflow_id=workflow_id,
                tool_name=tool_name,
                input_json=payload,
                output_json=exc.result,
                status="failed",
                pi_turn_id=pi_turn_id,
            )
            raise
        except Exception as exc:
            self.repository.record_tool_call(
                tool_call_id=tool_call_id,
                workflow_id=workflow_id,
                tool_name=tool_name,
                input_json=payload,
                output_json={"error": str(exc)},
                status="failed",
                pi_turn_id=pi_turn_id,
            )
            raise
