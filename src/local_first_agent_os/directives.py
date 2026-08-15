# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import contextlib
import re
import shlex
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Literal, cast

from .constants import DEFAULT_DISPATCHER_NAME
from .contracts import (
    GRAPH_SUBCOMMANDS,
    AgentHarness,
    DirectiveSpec,
    GraphSubcommand,
    ModelRole,
    WalkthruAction,
)
from .model_registry import ModelRegistry
from .settings import Settings

CHROME_ACTION_ALIASES: dict[str, str] = {
    "back": "back",
    "close": "close",
    "connect": "start",
    "console": "console",
    "create": "open",
    "ask": "decide",
    "decision": "decide",
    "decide": "decide",
    "delete": "close",
    "eval": "evaluate",
    "evaluate": "evaluate",
    "focus": "select",
    "forward": "forward",
    "gather": "gather",
    "go": "navigate",
    "js": "evaluate",
    "list": "list",
    "ls": "list",
    "navigate": "navigate",
    "network": "network",
    "new": "open",
    "open": "open",
    "reload": "reload",
    "read": "read",
    "screenshot": "screenshot",
    "select": "select",
    "snapshot": "snapshot",
    "start": "start",
    "status": "status",
    "stop": "stop",
    "summarize": "summarize",
    "summarise": "summarize",
    "summary": "summarize",
    "tabs": "list",
    "close-category": "close_category",
    "close-matching": "close_category",
    "close-tabs": "close_category",
}

DEFAULT_ALIASES: dict[str, str] = {
    "/base": "general",
    "/default": "general",
    "/general": "general",
    "/gemma4": "general",
    "/qwen": "general_fallback",
    "/qwen3.8": "general_fallback",
    "/qwen38": "general_fallback",
    "/glimmer": "deliberator",
    "/deliberator": "deliberator",
    "/surya": "ocr",
    "/ocr": "ocr",
    "/vision": "ocr",
    "/chandra": "hard_ocr",
    "/hard-ocr": "hard_ocr",
    "/asr": "asr",
    "/audio": "asr",
    "/med": "medical",
    "/medical": "medical",
    "/embed": "embedder",
    "/embedder": "embedder",
    "/store-embed": "embedder",
    "/fallback": "general_fallback",
    "/general-fallback": "general_fallback",
    "/general_fallback": "general_fallback",
    "/compactor": "compactor",
}

# Every role's canonical value is always a valid alias (e.g. /general_fallback),
# so printed hints like "pi /start /general_fallback" from ModelNotLoadedError
# and models-help can never drift from what the parser accepts.
for _role in ModelRole:
    DEFAULT_ALIASES.setdefault(f"/{_role.value}", _role.value)

# Aliases for the observability stack. These are not model roles — `/start` and
# `/stop` route them to scripts/start-local-observability.sh instead of the model registry.
OBSERVABILITY_ALIASES = {"/logging", "/logs", "/observability", "/telemetry"}
DISPATCHER_ALIAS = "/dispatcher"
NEW_PROJECT_ALIAS = "/new-project"
APPROVED_GAWD_ALIAS = "/approved-gawd"
APPROVE_MOST_RECENT_ALIAS = "/approve-most-recent"
TRY_MILESTONE_ALIAS = "/try-milestone"
DISPATCH_ALIAS = "/dispatch"
REVIEW_MERGE_ALIAS = "/review-merge"
APPROVE_MERGE_ALIAS = "/approve-merge"
DispatcherTier = Literal["junior", "senior", "staff"]

AGENT_QUERY_ALIASES: dict[str, AgentHarness] = {
    "/claude": AgentHarness.CLAUDE_CODE,
    "/cc": AgentHarness.CLAUDE_CODE,
    "/codex": AgentHarness.CODEX_CLI,
}

# Unioned rather than listed, so adding a harness alias above cannot leave the
# help surface calling it unrecognized.
TOP_LEVEL_DIRECTIVES = set(AGENT_QUERY_ALIASES) | {
    "/start",
    "/stop",
    "/status",
    "/project-status",
    "/get",
    "/fetch",
    "/compact",
    "/timer",
    "/store",
    "/embed",
    "/ocr",
    "/hard-ocr",
    "/screenshot",
    "/send-to-wf",
    "/done",
    "/chrome",
    "/ledger",
    "/read",
    "/graph",
    "/saga",
    "/pow-wow",
    "/ambiguity",
    "/stagnation",
    "/try-milestone",
    "/approve-most-recent",
    DISPATCH_ALIAS,
    "/review-merge",
    "/approve-merge",
}

DEFAULT_AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".wav",
    ".aac",
    ".flac",
    ".ogg",
    ".opus",
}

DEFAULT_TEXT_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".json",
    ".jsonl",
    ".csv",
    ".tsv",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".yaml",
    ".yml",
}

DEFAULT_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".heic",
    ".tif",
    ".tiff",
}

_MONTH_DAY_RE = re.compile(r"^(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])$")


def parse_month_day(token: str) -> str | None:
    match = _MONTH_DAY_RE.match(token.strip())
    if not match:
        return None
    month = int(match.group(1))
    day = int(match.group(2))
    return f"{month:02d}/{day:02d}"


class Parser(ABC):
    parser_name = "Parser"

    def __init__(self, settings: Settings):
        self.settings = settings

    @abstractmethod
    def parse(self, raw: str) -> Any:
        raise NotImplementedError

    def model_dump(self) -> dict[str, Any]:
        return {}

    def _split(self, raw: str) -> tuple[str, list[str]]:
        parts = shlex.split(raw)
        if not parts:
            raise ValueError(f"{self.parser_name} is empty.")
        return parts[0], parts[1:]

    def _truncate_text_to_tail(self, tail: list[str]) -> str | None:
        return " ".join(tail).strip() or None


class DirectiveParser(Parser):
    parser_name = "Directive"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        config = settings.load_toml(settings.directive_config_path)
        self.default_role = ModelRole(config.get("default_model_role", ModelRole.GENERAL.value))
        aliases = {**DEFAULT_ALIASES, **(config.get("aliases") or {})}
        self.aliases = {str(alias): ModelRole(role) for alias, role in aliases.items()}
        store = config.get("store") or {}
        self.text_extensions = set(store.get("text_extensions") or DEFAULT_TEXT_EXTENSIONS)
        self.image_extensions = set(store.get("image_extensions") or DEFAULT_IMAGE_EXTENSIONS)
        self.audio_extensions = set(store.get("audio_extensions") or DEFAULT_AUDIO_EXTENSIONS)
        self.max_file_bytes = int(store.get("max_file_bytes") or 2_000_000)
        ocr = config.get("ocr") or {}
        self.ocr_image_extensions = set(ocr.get("image_extensions") or self.image_extensions)
        self.ocr_max_file_bytes = int(ocr.get("max_file_bytes") or 50_000_000)
        # Fallback pixel budget for OCR roles whose registry entry declares no
        # ocr_max_dimension of its own.
        self.ocr_max_dimension = int(ocr.get("max_dimension") or 2_048)
        context = config.get("context") or {}
        self.compaction_threshold_ratio = float(context.get("compaction_threshold_ratio") or 0.9)
        self.compaction_target_ratio = float(context.get("compaction_target_ratio") or 0.5)
        default_window = context.get("default_max_window_tokens")
        if default_window is None:
            try:
                default_window = (
                    ModelRegistry(settings).resolve_model(ModelRole.GENERAL).context_window
                )
            except Exception:
                default_window = None
        self.default_max_window_tokens = int(default_window or 32768)

    def parse(self, raw: str) -> DirectiveSpec:
        command, tail = self._split(raw)
        if not command.startswith("/"):
            raise ValueError(
                "Directives must start with /start, /stop, /status, /get, /fetch, "
                "/compact, /timer, /store, /embed, /send-to-wf, /done, /chrome, "
                "/ledger, /saga, /pow-wow, "
                "/ambiguity, or /stagnation."
            )
        if command == "/status":
            return DirectiveSpec(raw=raw, action="status", alias="/status")
        if command == "/project-status":
            if len(tail) != 1:
                raise ValueError("/project-status requires exactly one linked project id.")
            return DirectiveSpec(
                raw=raw,
                action="project_status",
                alias="/project-status",
                target_project_id=tail[0],
            )
        if command == "/timer":
            if not tail:
                raise ValueError("/timer requires a duration, for example /timer 50.")
            return DirectiveSpec(
                raw=raw,
                action="timer",
                alias="/timer",
                query=self._truncate_text_to_tail(tail),
            )
        if command == "/fetch":
            return self._parse_fetch(raw, tail)
        if command == "/embed":
            return self._parse_store(raw, tail, alias="/embed")
        if command in {"/ocr", "/hard-ocr"}:
            role = ModelRole.HARD_OCR if command == "/hard-ocr" else ModelRole.OCR
            return self._parse_ocr(raw, tail, alias=command, model_role=role)
        if command in AGENT_QUERY_ALIASES:
            return self._parse_agent_query(raw, tail, alias=command)
        if command == "/send-to-wf":
            return self._parse_send_to_wf(raw, tail)
        if command == "/done":
            return DirectiveSpec(raw=raw, action="done", query=self._truncate_text_to_tail(tail))
        if command == "/chrome":
            return self._parse_chrome(raw, tail)
        if command == "/ledger":
            return self._parse_ledger(raw, tail, alias="/ledger")
        if command == "/read":
            if not tail or tail[0] not in {"ledger", "/ledger"}:
                raise ValueError("/read currently supports only /read /ledger.")
            return self._parse_ledger(raw, tail[1:], alias="/read /ledger")
        if command == "/graph":
            return self._parse_graph(raw, tail)
        if command == "/saga":
            return self._parse_saga(raw, tail)
        if command == "/pow-wow":
            return self._parse_pow_wow(raw, tail)
        if command == "/ambiguity":
            return self._parse_ambiguity(raw, tail)
        if command == "/stagnation":
            return self._parse_stagnation(raw, tail)
        if command == TRY_MILESTONE_ALIAS:
            return DirectiveSpec(
                raw=raw,
                action="try_milestone",
                alias=TRY_MILESTONE_ALIAS,
                query=self._truncate_text_to_tail(tail),
            )
        if command == APPROVE_MOST_RECENT_ALIAS:
            if tail:
                raise ValueError(
                    "/approve-most-recent takes no arguments; it resolves the latest "
                    "dependency-ready milestone from the ledger."
                )
            return DirectiveSpec(
                raw=raw,
                action="approve_most_recent",
                alias=APPROVE_MOST_RECENT_ALIAS,
            )
        if command == DISPATCH_ALIAS:
            if tail:
                raise ValueError(
                    f"{command} takes no arguments; use /start /dispatcher for "
                    "custom dispatcher options."
                )
            return DirectiveSpec(
                raw=raw,
                action="dispatch_once",
                alias=DISPATCH_ALIAS,
                dispatcher_max_polls=1,
            )
        if command == REVIEW_MERGE_ALIAS:
            if len(tail) > 1:
                raise ValueError("/review-merge accepts at most one optional approval_id.")
            return DirectiveSpec(
                raw=raw,
                action="review_merge",
                alias=REVIEW_MERGE_ALIAS,
                query=self._truncate_text_to_tail(tail),
            )
        if command == APPROVE_MERGE_ALIAS:
            if len(tail) != 1:
                raise ValueError(
                    "/approve-merge requires the exact approval_id printed by /review-merge."
                )
            return DirectiveSpec(
                raw=raw,
                action="approve_merge",
                alias=APPROVE_MERGE_ALIAS,
                query=tail[0],
            )
        action = command[1:]
        if action not in {"start", "stop", "get", "compact", "timer", "store", "screenshot"}:
            raise ValueError(f"Unsupported directive: {command}")
        if action == "start":
            return self._parse_start(raw, tail)
        if action == "stop":
            return self._parse_stop(raw, tail)
        if action == "get":
            return DirectiveSpec(raw=raw, action="get", query=self._truncate_text_to_tail(tail))
        if action == "store":
            return self._parse_store(raw, tail, alias="/store")
        if action == "screenshot":
            return self._parse_screenshot(raw, tail)
        return DirectiveSpec(raw=raw, action="compact", query=self._truncate_text_to_tail(tail))

    def _parse_fetch(self, raw: str, tail: list[str]) -> DirectiveSpec:
        if not tail or tail[0] != "/workflowy":
            raise ValueError(
                "/fetch currently requires the /workflowy source selector. "
                "Usage: /fetch /workflowy <query>"
            )
        query = self._truncate_text_to_tail(tail[1:])
        if query is None:
            raise ValueError("/fetch /workflowy expects a query string.")
        return DirectiveSpec(
            raw=raw,
            action="fetch",
            alias="/fetch /workflowy",
            query=query,
            retrieval_source="workflowy",
        )

    def _parse_ledger(self, raw: str, tail: list[str], *, alias: str) -> DirectiveSpec:
        return DirectiveSpec(
            raw=raw,
            action="ledger",
            alias=alias,
            query_tail=self._truncate_text_to_tail(tail),
        )

    def _parse_chrome(self, raw: str, tail: list[str]) -> DirectiveSpec:
        if not tail:
            return DirectiveSpec(
                raw=raw,
                action="chrome",
                alias="/chrome",
                chrome_action="list",
            )
        requested_action = tail[0].lower()
        action = CHROME_ACTION_ALIASES.get(requested_action)
        if action is None:
            allowed = ", ".join(sorted(CHROME_ACTION_ALIASES))
            raise ValueError(f"/chrome action must be one of: {allowed}; got {tail[0]}")
        return DirectiveSpec(
            raw=raw,
            action="chrome",
            alias="/chrome",
            chrome_action=action,
            chrome_args=tuple(tail[1:]),
        )

    def _parse_agent_query(self, raw: str, tail: list[str], *, alias: str) -> DirectiveSpec:
        query = self._truncate_text_to_tail(tail)
        if not query:
            raise ValueError(f"{alias} requires a query, for example {alias} explain this repo.")
        return DirectiveSpec(
            raw=raw,
            action="agent_query",
            alias=alias,
            agent_harness=AGENT_QUERY_ALIASES[alias],
            query=query,
        )

    def _parse_send_to_wf(self, raw: str, tail: list[str]) -> DirectiveSpec:
        if len(tail) < 2:
            raise ValueError(
                "/send-to-wf requires a file path and a month/day token "
                "(for example /send-to-wf ~/note.txt 04/28)."
            )
        month_day_token = tail[-1]
        month_day = parse_month_day(month_day_token)
        if month_day is None:
            raise ValueError(f"/send-to-wf expects a final MM/DD argument; got: {month_day_token}")
        path_tokens = tail[:-1]
        path = Path(" ".join(path_tokens)).expanduser()
        return DirectiveSpec(
            raw=raw,
            action="send_to_wf",
            path=path,
            alias="/send-to-wf",
            month_day=month_day,
        )

    def _parse_start(self, raw: str, tail: list[str]) -> DirectiveSpec:
        if not tail:
            raise ValueError(
                "/start requires a model alias or /logging (for example /start /qwen, "
                "/start /embed, /start /logging). Bare /start is no longer supported."
            )
        if tail[0] == "/store":
            return self._parse_store(raw, tail[1:], alias="/store")
        if tail[0] == NEW_PROJECT_ALIAS:
            return self._parse_new_project(raw, tail[1:])
        if tail[0] == APPROVED_GAWD_ALIAS:
            if len(tail) < 2:
                raise ValueError(
                    "/start /approved-gawd requires one final_gawd_doc_id. "
                    "Usage: /start /approved-gawd <final_gawd_doc_id> "
                    "--target-project <project_id>"
                )
            gawd_doc_id = tail[1]
            target_project_id: str | None = None
            create_target_id: str | None = None
            i = 2
            while i < len(tail):
                token = tail[i]
                if token in {"--target-project", "--target-project-id"} and i + 1 < len(tail):
                    target_project_id = tail[i + 1]
                    i += 2
                elif token == "--create-target" and i + 1 < len(tail):
                    create_target_id = tail[i + 1]
                    i += 2
                else:
                    raise ValueError(
                        "/start /approved-gawd accepts --target-project <project_id> "
                        "or --create-target <project_id> after the final_gawd_doc_id"
                    )
            if target_project_id and create_target_id:
                raise ValueError("Choose either --target-project or --create-target, not both.")
            return DirectiveSpec(
                raw=raw,
                action="approved_gawd",
                alias=APPROVED_GAWD_ALIAS,
                query=gawd_doc_id,
                target_project_id=target_project_id,
                create_target_id=create_target_id,
            )
        if tail[0] == DISPATCHER_ALIAS:
            return self._parse_dispatcher(raw, tail[1:])
        if tail[0] in OBSERVABILITY_ALIASES:
            return DirectiveSpec(
                raw=raw,
                action="observability",
                alias=tail[0],
                query="up",
            )
        alias = tail[0]
        role = self.aliases.get(alias)
        if role is None:
            role = self._infer_role_from_model_directory(alias)
        query_tail = self._truncate_text_to_tail(tail[1:])
        return DirectiveSpec(
            raw=raw,
            action="start",
            model_role=role,
            alias=alias,
            query_tail=query_tail,
        )

    def _parse_new_project(self, raw: str, tail: list[str]) -> DirectiveSpec:
        path_tokens: list[str] = []
        target_project_id: str | None = None
        create_target_id: str | None = None
        walkthru = False
        no_walkthru = False
        walkthru_id: str | None = None
        walkthru_action: WalkthruAction | None = None
        walkthru_section_id: str | None = None
        walkthru_text: str | None = None
        index = 0
        while index < len(tail):
            token = tail[index]
            if token in {"--target-project", "--target-project-id"}:
                if index + 1 >= len(tail):
                    raise ValueError(f"{token} requires a project id.")
                target_project_id = tail[index + 1]
                index += 2
                continue
            if token == "--create-target":
                if index + 1 >= len(tail):
                    raise ValueError("--create-target requires a project id.")
                create_target_id = tail[index + 1]
                index += 2
                continue
            if token == "--walkthru":
                walkthru = True
                index += 1
                if index < len(tail) and not tail[index].startswith("--"):
                    walkthru_id = tail[index]
                    index += 1
                continue
            if token == "--no-walkthru":
                no_walkthru = True
                index += 1
                continue
            if token in {"--answer", "--revise"}:
                if not walkthru:
                    raise ValueError(f"{token} requires --walkthru.")
                if walkthru_action is not None:
                    raise ValueError("Choose exactly one walkthru action per command.")
                walkthru_action = cast(WalkthruAction, token.removeprefix("--"))
                if index + 1 >= len(tail):
                    raise ValueError(f"{token} requires text.")
                walkthru_text = " ".join(tail[index + 1 :]).strip()
                if not walkthru_text:
                    raise ValueError(f"{token} requires text.")
                index = len(tail)
                continue
            if token == "--edit":
                if not walkthru:
                    raise ValueError("--edit requires --walkthru.")
                if walkthru_action is not None:
                    raise ValueError("Choose exactly one walkthru action per command.")
                if index + 2 >= len(tail):
                    raise ValueError("--edit requires a section id and corrected summary.")
                walkthru_action = "edit"
                walkthru_section_id = tail[index + 1]
                walkthru_text = " ".join(tail[index + 2 :]).strip()
                if not walkthru_text:
                    raise ValueError("--edit requires a corrected summary.")
                index = len(tail)
                continue
            if token in {"--accept", "--skip", "--status", "--finish"}:
                if not walkthru:
                    raise ValueError(f"{token} requires --walkthru.")
                if walkthru_action is not None:
                    raise ValueError("Choose exactly one walkthru action per command.")
                walkthru_action = cast(WalkthruAction, token.removeprefix("--"))
                index += 1
                continue
            if token.startswith("--"):
                raise ValueError(
                    "/start /new-project starts a walkthru by default. It also accepts "
                    "a draft path, --target-project-id <project_id>, or --no-walkthru "
                    "for a blank draft template."
                )
            path_tokens.append(token)
            index += 1
        if walkthru and no_walkthru:
            raise ValueError("Choose either --walkthru or --no-walkthru, not both.")
        # A draft path means the draft already exists, so only the pathless form
        # has anything to walk through. That form now starts the walkthru, and
        # --no-walkthru is what still emits the blank template to fill in by hand.
        if not walkthru and not no_walkthru and not path_tokens:
            walkthru = True
        if walkthru:
            if target_project_id and create_target_id:
                raise ValueError("Choose either --target-project-id or --create-target, not both.")
            action = walkthru_action or "start"
            if path_tokens:
                raise ValueError("--walkthru does not accept a draft path.")
            if action != "start" and walkthru_id is None:
                raise ValueError(
                    f"--{action} requires the walkthru id printed by the start command."
                )
            if action == "start" and walkthru_id is not None:
                raise ValueError(
                    "A walkthru id requires --answer, --accept, --revise, --skip, "
                    "--edit, --status, or --finish."
                )
            return DirectiveSpec(
                raw=raw,
                action="new_project",
                alias=NEW_PROJECT_ALIAS,
                target_project_id=target_project_id,
                create_target_id=create_target_id,
                walkthru_action=action,
                walkthru_id=walkthru_id,
                walkthru_section_id=walkthru_section_id,
                walkthru_text=walkthru_text,
            )
        path = Path(" ".join(path_tokens)).expanduser() if path_tokens else None
        if target_project_id and create_target_id:
            raise ValueError("Choose either --target-project-id or --create-target, not both.")
        return DirectiveSpec(
            raw=raw,
            action="new_project",
            alias=NEW_PROJECT_ALIAS,
            path=path,
            target_project_id=target_project_id,
            create_target_id=create_target_id,
        )

    def _parse_dispatcher(self, raw: str, tail: list[str]) -> DirectiveSpec:
        name = DEFAULT_DISPATCHER_NAME
        tier: DispatcherTier | None = None
        interval_seconds = 2.0
        max_polls: int | None = None
        i = 0
        while i < len(tail):
            token = tail[i]
            if token == "--name" and i + 1 < len(tail):
                name = tail[i + 1]
                i += 2
            elif token == "--tier" and i + 1 < len(tail):
                raw_tier = tail[i + 1]
                if raw_tier not in {"junior", "senior", "staff"}:
                    raise ValueError("/start /dispatcher --tier must be junior, senior, or staff")
                tier = cast(DispatcherTier, raw_tier)
                i += 2
            elif token == "--interval" and i + 1 < len(tail):
                try:
                    interval_seconds = float(tail[i + 1])
                except ValueError as exc:
                    raise ValueError("/start /dispatcher --interval must be a number") from exc
                if interval_seconds <= 0:
                    raise ValueError("/start /dispatcher --interval must be positive")
                i += 2
            elif token == "--max-polls" and i + 1 < len(tail):
                try:
                    max_polls = int(tail[i + 1])
                except ValueError as exc:
                    raise ValueError("/start /dispatcher --max-polls must be an integer") from exc
                if max_polls <= 0:
                    raise ValueError("/start /dispatcher --max-polls must be positive")
                i += 2
            else:
                raise ValueError(
                    "/start /dispatcher accepts --name, --tier, --interval, and --max-polls"
                )
        if max_polls is None:
            raise ValueError(
                "/start /dispatcher requires --max-polls; use /dispatch for one bounded poll"
            )
        return DirectiveSpec(
            raw=raw,
            action="dispatcher",
            alias=DISPATCHER_ALIAS,
            dispatcher_name=name,
            dispatcher_tier=tier,
            dispatcher_interval_seconds=interval_seconds,
            dispatcher_max_polls=max_polls,
        )

    def _parse_stop(self, raw: str, tail: list[str]) -> DirectiveSpec:
        if not tail:
            return DirectiveSpec(raw=raw, action="stop")
        if tail[0] in OBSERVABILITY_ALIASES:
            return DirectiveSpec(
                raw=raw,
                action="observability",
                alias=tail[0],
                query="down",
            )
        alias = tail[0]
        role = self.aliases.get(alias) or self._infer_role_from_model_directory(alias)
        query_tail = self._truncate_text_to_tail(tail[1:])
        return DirectiveSpec(
            raw=raw,
            action="stop",
            model_role=role,
            alias=alias,
            query_tail=query_tail,
        )

    def _parse_store(self, raw: str, tail: list[str], alias: str) -> DirectiveSpec:
        remote = False
        rest = tail
        if rest and rest[0] == "/remote":
            remote = True
            rest = rest[1:]
        if not rest:
            raise ValueError(f"{alias} requires a local file or directory path.")
        return DirectiveSpec(
            raw=raw,
            action="store",
            model_role=ModelRole.EMBEDDER,
            path=Path(" ".join(rest)).expanduser(),
            remote=remote,
            alias=alias,
        )

    def _parse_screenshot(self, raw: str, tail: list[str]) -> DirectiveSpec:
        if not tail:
            raise ValueError("/screenshot requires an image path.")
        query_tail = self._truncate_text_to_tail(tail[1:])
        return DirectiveSpec(
            raw=raw,
            action="screenshot",
            model_role=ModelRole.OCR,
            path=Path(tail[0]).expanduser(),
            alias="/screenshot",
            query_tail=query_tail,
        )

    def _parse_ocr(
        self,
        raw: str,
        tail: list[str],
        *,
        alias: str,
        model_role: ModelRole,
    ) -> DirectiveSpec:
        if len(tail) != 1:
            raise ValueError(f"{alias} requires exactly one absolute image or directory path.")
        path = Path(tail[0])
        if not path.is_absolute():
            raise ValueError(
                f"{alias} requires an absolute path; ~ and relative paths are not accepted."
            )
        return DirectiveSpec(
            raw=raw,
            action="ocr_capture",
            model_role=model_role,
            path=path,
            alias=alias,
        )

    def _infer_role_from_model_directory(self, token: str) -> ModelRole:
        normalized = token[1:] if token.startswith("/") else token
        models_dir = self.settings.llama_models_dir.expanduser()
        if not (models_dir / normalized).exists():
            raise ValueError(f"Unknown model alias or local model directory: {token}")
        if "embed" in normalized:
            return ModelRole.EMBEDDER
        if "compact" in normalized:
            return ModelRole.COMPACTOR
        if "fallback" in normalized or "gemma4" in normalized:
            return ModelRole.GENERAL_FALLBACK
        if "med" in normalized:
            return ModelRole.MEDICAL
        if "asr" in normalized or "audio" in normalized:
            return ModelRole.ASR
        if "chandra" in normalized or "hard-ocr" in normalized or "hard_ocr" in normalized:
            return ModelRole.HARD_OCR
        if "ocr" in normalized or "surya" in normalized or "vision" in normalized:
            return ModelRole.OCR
        return ModelRole.GENERAL

    def _parse_graph(self, raw: str, tail: list[str]) -> DirectiveSpec:
        """Parse `/graph <subcommand> [argument]`.

        Every subcommand is named in the error rather than guessed at: the
        operator reaches for this directive precisely when the graph is not
        behaving, which is the worst moment to silently do the wrong thing.
        """
        usage = f"/graph requires one of: {', '.join(GRAPH_SUBCOMMANDS)}."
        if not tail:
            raise ValueError(usage)
        try:
            subcommand = GraphSubcommand(tail[0])
        except ValueError as exc:
            raise ValueError(f"Unsupported /graph subcommand: {tail[0]}. {usage}") from exc

        argument = self._truncate_text_to_tail(tail[1:])
        if subcommand in {GraphSubcommand.GET, GraphSubcommand.NODE} and not argument:
            raise ValueError(
                f"/graph {subcommand.value} requires a quoted argument, "
                f'for example: /graph {subcommand.value} "the gawd doc".'
            )
        if subcommand is GraphSubcommand.BUILD:
            return DirectiveSpec(
                raw=raw,
                action="graph",
                alias="/graph",
                graph_subcommand=subcommand,
                path=Path(argument).expanduser() if argument else None,
            )
        return DirectiveSpec(
            raw=raw,
            action="graph",
            alias="/graph",
            graph_subcommand=subcommand,
            query=argument,
        )

    def _parse_saga(self, raw: str, tail: list[str]) -> DirectiveSpec:
        """Parse /saga <goal> [--budget <tokens>] [--executor <backend>]

        Examples:
          /saga Build a widget that does X
          /saga Build a widget --budget 500000
          /saga --executor fake_process Build a widget
          /saga --executor cli Build a widget
        """
        budget: int | None = None
        executor_backend: str | None = None
        worktree_root: Path | None = None
        filtered: list[str] = []
        i = 0
        while i < len(tail):
            if tail[i] == "--budget" and i + 1 < len(tail):
                with contextlib.suppress(ValueError):
                    budget = int(tail[i + 1])
                i += 2
            elif tail[i] == "--executor" and i + 1 < len(tail):
                executor_backend = tail[i + 1]
                i += 2
            elif tail[i] == "--worktree-root" and i + 1 < len(tail):
                worktree_root = Path(tail[i + 1]).expanduser()
                i += 2
            else:
                filtered.append(tail[i])
                i += 1
        if executor_backend not in (None, "dry_run", "fake_process", "cli"):
            raise ValueError("/saga --executor must be one of: dry_run, fake_process, cli")
        goal = " ".join(filtered).strip()
        if not goal:
            raise ValueError("/saga requires a goal description. Usage: /saga <goal>")
        return DirectiveSpec(
            raw=raw,
            action="saga",
            alias="/saga",
            query=goal,
            budget_tokens=budget,
            saga_executor_backend=executor_backend,
            saga_worktree_root=worktree_root,
        )

    def _parse_pow_wow(self, raw: str, tail: list[str]) -> DirectiveSpec:
        """Parse /pow-wow <saga_id> <stage> <goal>

        Examples:
          /pow-wow abc123 IMPLEMENTATION Implement the widget service
        """
        if len(tail) < 3:
            raise ValueError(
                "/pow-wow requires: /pow-wow <saga_id> <stage> <goal>\n"
                "Stages: IDEA_INTAKE | GAWD_DOC | REQUIREMENT_DECOMPOSITION | "
                "IMPLEMENTATION | REVIEW_EVALUATION | USER_APPROVAL"
            )
        saga_id = tail[0]
        stage = tail[1].upper()
        goal = " ".join(tail[2:]).strip()
        return DirectiveSpec(
            raw=raw,
            action="pow_wow",
            alias="/pow-wow",
            saga_id=saga_id,
            pow_wow_stage=stage,
            query=goal,
        )

    def _parse_ambiguity(self, raw: str, tail: list[str]) -> DirectiveSpec:
        """Parse /ambiguity <gawd_doc_id>

        Checks clarity scores for the given GAWD doc.
        """
        if not tail:
            raise ValueError("/ambiguity requires a gawd_doc_id. Usage: /ambiguity <gawd_doc_id>")
        return DirectiveSpec(
            raw=raw,
            action="ambiguity_check",
            alias="/ambiguity",
            query=tail[0],
        )

    def _parse_stagnation(self, raw: str, tail: list[str]) -> DirectiveSpec:
        """Parse /stagnation <saga_id>

        Checks whether a saga is spinning without progress.
        """
        if not tail:
            raise ValueError("/stagnation requires a saga_id. Usage: /stagnation <saga_id>")
        return DirectiveSpec(
            raw=raw,
            action="stagnation_check",
            alias="/stagnation",
            query=tail[0],
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "default_role": self.default_role.value,
            "aliases": {alias: role.value for alias, role in self.aliases.items()},
            "text_extensions": sorted(self.text_extensions),
            "image_extensions": sorted(self.image_extensions),
            "audio_extensions": sorted(self.audio_extensions),
            "max_file_bytes": self.max_file_bytes,
            "compaction_threshold_ratio": self.compaction_threshold_ratio,
            "compaction_target_ratio": self.compaction_target_ratio,
            "default_max_window_tokens": self.default_max_window_tokens,
        }
