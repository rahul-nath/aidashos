# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed boundary for host-owned responsive browser acceptance.

This module intentionally does not expose an ambient browser to a model. A
senior or staff task may depend on the resulting evidence, while the host owns
URL policy, viewport bounds, capture, redaction, persistence, and timeouts.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import tempfile
import time
from contextlib import suppress
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from .toolchains import project_environment


class BrowserAcceptanceStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class BrowserViewport(BaseModel, frozen=True):
    name: str = Field(min_length=1, max_length=40)
    width: int = Field(ge=320, le=3840)
    height: int = Field(ge=480, le=2160)
    device_scale_factor: float = Field(default=1.0, ge=1.0, le=3.0)


class BrowserAcceptanceRequest(BaseModel, frozen=True):
    schema_version: str = "browser_acceptance_request.v2"
    target_url: str = Field(min_length=8, max_length=2048)
    viewports: tuple[BrowserViewport, ...]
    required_paths: tuple[str, ...] = ("/",)
    screenshot_full_page: bool = True
    capture_console: bool = True
    capture_failed_requests: bool = True
    allowed_hosts: tuple[str, ...] = ()
    required_selectors: tuple[str, ...] = ()
    bounded_selectors: tuple[str, ...] = ()
    timeout_seconds: int = Field(default=60, ge=5, le=300)

    @field_validator("target_url")
    @classmethod
    def validate_target_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("target_url must use http or https")
        return value.rstrip("/")

    @field_validator("required_paths")
    @classmethod
    def validate_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("required_paths must not be empty")
        if any(not value.startswith("/") for value in values):
            raise ValueError("required_paths entries must start with /")
        return values

    @field_validator("allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 16:
            raise ValueError("allowed_hosts is bounded to sixteen entries")
        normalized = tuple(value.strip().casefold() for value in values)
        if any(not value or "/" in value or ":" in value for value in normalized):
            raise ValueError("allowed_hosts entries must be hostnames without ports")
        return normalized

    @field_validator("required_selectors", "bounded_selectors")
    @classmethod
    def validate_selectors(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) > 16:
            raise ValueError("selector lists are bounded to sixteen entries")
        if any(not value.strip() or len(value) > 200 for value in values):
            raise ValueError("selectors must be non-empty and at most 200 characters")
        return values

    @model_validator(mode="after")
    def validate_viewports(self) -> BrowserAcceptanceRequest:
        if not self.viewports:
            raise ValueError("viewports must not be empty")
        if len(self.viewports) > 8:
            raise ValueError("viewports are bounded to eight entries")
        if len({viewport.name for viewport in self.viewports}) != len(self.viewports):
            raise ValueError("viewport names must be unique")
        return self


class BrowserCaptureEvidence(BaseModel, frozen=True):
    path: str
    viewport: BrowserViewport
    screenshot_artifact_id: str | None = None
    trace_artifact_id: str | None = None
    horizontal_overflow: bool
    final_url: str | None = None
    page_title: str | None = None
    response_status: int | None = None
    duration_ms: int = 0
    console_errors: tuple[str, ...] = ()
    page_errors: tuple[str, ...] = ()
    failed_requests: tuple[str, ...] = ()
    assertion_failures: tuple[str, ...] = ()
    cancelled: bool = False


class BrowserAcceptanceEvidence(BaseModel, frozen=True):
    schema_version: str = "browser_acceptance_evidence.v2"
    request_artifact_id: str
    status: BrowserAcceptanceStatus
    captures: tuple[BrowserCaptureEvidence, ...]
    summary: str


class BrowserAcceptanceRun(BaseModel, frozen=True):
    schema_version: str = "browser_acceptance_run.v2"
    request_artifact_id: str
    evidence_artifact_id: str
    evidence: BrowserAcceptanceEvidence


class PreviewProcessEvidence(BaseModel, frozen=True):
    schema_version: str = "preview_process_evidence.v1"
    command: str
    cwd: str
    target_url: str
    pid: int | None
    ready: bool
    returncode: int | None = None
    direct_child_reaped: bool = False
    process_group_reaped: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""


class BrowserAcceptanceArtifactWriter(Protocol):
    def write_json(
        self,
        *,
        role: str,
        payload: dict[str, Any] | list[Any],
        workflow_id: str | None,
        schema_version: str,
    ) -> Any: ...

    def write_bytes(
        self,
        *,
        role: str,
        data: bytes,
        workflow_id: str | None,
        mime_type: str,
        schema_version: str,
        extension: str | None = None,
    ) -> Any: ...


class BrowserAcceptanceCancellation(Protocol):
    def is_set(self) -> bool: ...


def _artifact_id(value: Any) -> str:
    artifact_id = getattr(value, "artifact_id", None)
    if not isinstance(artifact_id, str) or not artifact_id:
        raise RuntimeError("browser artifact persistence returned no artifact_id")
    return artifact_id


def _bounded_message(value: object, *, limit: int = 500) -> str:
    text = str(value).replace("\n", " ").strip()
    return text[:limit]


class BrowserAcceptanceRunner:
    """Run an isolated, anonymous Playwright inspection owned by the host."""

    def __init__(self, artifact_writer: BrowserAcceptanceArtifactWriter):
        self.artifact_writer = artifact_writer

    def run(
        self,
        request: BrowserAcceptanceRequest,
        *,
        workflow_id: str,
        cancel_event: BrowserAcceptanceCancellation | None = None,
    ) -> BrowserAcceptanceRun:
        request_ref = self.artifact_writer.write_json(
            role="browser_acceptance_request",
            payload=request.model_dump(mode="json"),
            workflow_id=workflow_id,
            schema_version=request.schema_version,
        )
        request_artifact_id = _artifact_id(request_ref)
        captures: list[BrowserCaptureEvidence] = []
        launch_error: str | None = None
        cancelled = self._cancelled(cancel_event)
        try:
            from playwright.sync_api import sync_playwright

            if not cancelled:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        for path in request.required_paths:
                            for viewport in request.viewports:
                                if self._cancelled(cancel_event):
                                    cancelled = True
                                    break
                                capture = self._capture_page_evidence(
                                    browser=browser,
                                    request=request,
                                    path=path,
                                    viewport=viewport,
                                    workflow_id=workflow_id,
                                    cancel_event=cancel_event,
                                )
                                captures.append(capture)
                                if capture.cancelled or self._cancelled(cancel_event):
                                    cancelled = True
                                    break
                            if cancelled:
                                break
                    finally:
                        browser.close()
        except Exception as exc:  # noqa: BLE001 - unavailable host capability is BLOCKED
            launch_error = _bounded_message(exc)

        expected_count = len(request.required_paths) * len(request.viewports)
        failed = [capture for capture in captures if self._capture_failed(capture)]
        if cancelled:
            status = BrowserAcceptanceStatus.CANCELLED
            summary = (
                f"Browser acceptance was cancelled after {len(captures)}/"
                f"{expected_count} required captures."
            )
        elif launch_error is not None:
            status = BrowserAcceptanceStatus.BLOCKED
            summary = f"Browser acceptance could not start: {launch_error}"
        elif len(captures) != expected_count:
            status = BrowserAcceptanceStatus.FAILED
            summary = (
                f"Browser acceptance captured {len(captures)}/{expected_count} "
                "required path/viewport combinations."
            )
        elif failed:
            status = BrowserAcceptanceStatus.FAILED
            summary = (
                f"Browser acceptance failed {len(failed)}/{expected_count} captures; "
                "inspect overflow, console, page-exception, network, trace, and "
                "selector evidence."
            )
        else:
            status = BrowserAcceptanceStatus.PASSED
            summary = f"Browser acceptance passed all {expected_count} required captures."
        evidence = BrowserAcceptanceEvidence(
            request_artifact_id=request_artifact_id,
            status=status,
            captures=tuple(captures),
            summary=summary,
        )
        evidence_ref = self.artifact_writer.write_json(
            role="browser_acceptance_evidence",
            payload=evidence.model_dump(mode="json"),
            workflow_id=workflow_id,
            schema_version=evidence.schema_version,
        )
        return BrowserAcceptanceRun(
            request_artifact_id=request_artifact_id,
            evidence_artifact_id=_artifact_id(evidence_ref),
            evidence=evidence,
        )

    def _capture_page_evidence(
        self,
        *,
        browser: Any,
        request: BrowserAcceptanceRequest,
        path: str,
        viewport: BrowserViewport,
        workflow_id: str,
        cancel_event: BrowserAcceptanceCancellation | None,
    ) -> BrowserCaptureEvidence:
        started = time.monotonic()
        console_errors: list[str] = []
        page_errors: list[str] = []
        failed_requests: list[str] = []
        assertion_failures: list[str] = []
        screenshot_artifact_id: str | None = None
        trace_artifact_id: str | None = None
        final_url: str | None = None
        title: str | None = None
        response_status: int | None = None
        overflow = False
        cancelled = False
        allowed_hosts = set(request.allowed_hosts)
        target_host = (urlparse(request.target_url).hostname or "").casefold()
        allowed_hosts.add(target_host)
        target_url = urljoin(request.target_url + "/", path.lstrip("/"))
        context = browser.new_context(
            viewport={"width": viewport.width, "height": viewport.height},
            device_scale_factor=viewport.device_scale_factor,
        )
        trace_directory = tempfile.TemporaryDirectory(prefix="browser-acceptance-trace-")
        trace_path = Path(trace_directory.name) / "trace.zip"
        trace_started = False
        try:
            context.tracing.start(screenshots=True, snapshots=True, sources=False)
            trace_started = True
        except Exception as exc:  # noqa: BLE001 - missing trace is terminal evidence
            assertion_failures.append(f"trace start failed: {_bounded_message(exc)}")
        page = context.new_page()
        page.on(
            "pageerror",
            lambda error: (
                page_errors.append(_bounded_message(error)) if len(page_errors) < 20 else None
            ),
        )
        if request.capture_console:
            page.on(
                "console",
                lambda message: (
                    console_errors.append(_bounded_message(message.text))
                    if message.type == "error" and len(console_errors) < 20
                    else None
                ),
            )
        if request.capture_failed_requests:
            page.on(
                "requestfailed",
                lambda failed: (
                    failed_requests.append(
                        _bounded_message(
                            f"{failed.method} {failed.url}: {failed.failure or 'failed'}"
                        )
                    )
                    if len(failed_requests) < 20
                    else None
                ),
            )
            page.on(
                "response",
                lambda response: (
                    failed_requests.append(
                        _bounded_message(
                            f"HTTP {response.status} {response.request.method} {response.url}"
                        )
                    )
                    if response.status >= 400 and len(failed_requests) < 20
                    else None
                ),
            )

        def route_navigation(route: Any) -> None:
            navigation_host = (urlparse(route.request.url).hostname or "").casefold()
            if route.request.is_navigation_request() and navigation_host not in allowed_hosts:
                if len(failed_requests) < 20:
                    failed_requests.append(
                        _bounded_message(
                            f"blocked navigation outside allowlist: {route.request.url}"
                        )
                    )
                route.abort()
                return
            route.continue_()

        page.route("**/*", route_navigation)
        try:
            response = page.goto(
                target_url,
                wait_until="load",
                timeout=request.timeout_seconds * 1000,
            )
            response_status = response.status if response is not None else None
            page.wait_for_timeout(50)
            cancelled = self._cancelled(cancel_event)
            final_url = str(page.url)
            title = page.title()
            final_host = (urlparse(final_url).hostname or "").casefold()
            if final_host not in allowed_hosts:
                assertion_failures.append(f"final URL host is not allowlisted: {final_host}")
            if response_status is None or response_status >= 400:
                assertion_failures.append(f"page response status was {response_status}")
            overflow = bool(
                page.evaluate(
                    "document.documentElement.scrollWidth > "
                    "document.documentElement.clientWidth + 1"
                )
            )
            assertion_failures.extend(self._selector_failures(page, request, viewport))
        except Exception as exc:  # noqa: BLE001 - capture failure becomes evidence
            assertion_failures.append(f"browser capture error: {_bounded_message(exc)}")
        try:
            screenshot = page.screenshot(full_page=request.screenshot_full_page)
            screenshot_ref = self.artifact_writer.write_bytes(
                role="browser_screenshot",
                data=screenshot,
                workflow_id=workflow_id,
                mime_type="image/png",
                schema_version="browser_screenshot.v1",
                extension="png",
            )
            screenshot_artifact_id = _artifact_id(screenshot_ref)
        except Exception as exc:  # noqa: BLE001 - screenshot failure is terminal evidence
            assertion_failures.append(f"screenshot failed: {_bounded_message(exc)}")
        finally:
            if trace_started:
                try:
                    context.tracing.stop(path=str(trace_path))
                    trace_ref = self.artifact_writer.write_bytes(
                        role="browser_trace",
                        data=trace_path.read_bytes(),
                        workflow_id=workflow_id,
                        mime_type="application/zip",
                        schema_version="browser_trace.v1",
                        extension="zip",
                    )
                    trace_artifact_id = _artifact_id(trace_ref)
                except Exception as exc:  # noqa: BLE001 - trace failure is terminal evidence
                    assertion_failures.append(f"trace persistence failed: {_bounded_message(exc)}")
            context.close()
            trace_directory.cleanup()
        return BrowserCaptureEvidence(
            path=path,
            viewport=viewport,
            screenshot_artifact_id=screenshot_artifact_id,
            trace_artifact_id=trace_artifact_id,
            horizontal_overflow=overflow,
            final_url=final_url,
            page_title=title,
            response_status=response_status,
            duration_ms=int((time.monotonic() - started) * 1000),
            console_errors=tuple(console_errors),
            page_errors=tuple(page_errors),
            failed_requests=tuple(failed_requests),
            assertion_failures=tuple(assertion_failures),
            cancelled=cancelled,
        )

    @staticmethod
    def _cancelled(cancel_event: BrowserAcceptanceCancellation | None) -> bool:
        return cancel_event is not None and cancel_event.is_set()

    @staticmethod
    def _selector_failures(
        page: Any,
        request: BrowserAcceptanceRequest,
        viewport: BrowserViewport,
    ) -> list[str]:
        failures: list[str] = []
        for selector in request.required_selectors:
            locator = page.locator(selector).first
            if locator.count() == 0 or not locator.is_visible():
                failures.append(f"required selector is missing or hidden: {selector}")
        for selector in request.bounded_selectors:
            locator = page.locator(selector).first
            if locator.count() == 0:
                failures.append(f"bounded selector is missing: {selector}")
                continue
            box = locator.bounding_box()
            if box is None:
                failures.append(f"bounded selector has no visible box: {selector}")
                continue
            horizontally_clipped = box["x"] < -1 or box["x"] + box["width"] > viewport.width + 1
            parent_clipped = bool(
                locator.evaluate(
                    """element => {
                      const own = element.getBoundingClientRect();
                      const parent = element.parentElement?.getBoundingClientRect();
                      return parent ? own.top < parent.top - 1 || own.bottom > parent.bottom + 1 ||
                        own.left < parent.left - 1 || own.right > parent.right + 1 : false;
                    }"""
                )
            )
            if horizontally_clipped or parent_clipped:
                failures.append(f"bounded selector is clipped: {selector}")
        return failures

    @staticmethod
    def _capture_failed(capture: BrowserCaptureEvidence) -> bool:
        return bool(
            capture.screenshot_artifact_id is None
            or capture.trace_artifact_id is None
            or capture.horizontal_overflow
            or capture.console_errors
            or capture.page_errors
            or capture.failed_requests
            or capture.assertion_failures
            or capture.cancelled
        )


class PreviewStartError(RuntimeError):
    def __init__(self, message: str, evidence: PreviewProcessEvidence):
        super().__init__(message)
        self.evidence = evidence


class LocalPreviewSession:
    """Own one local preview child process and its complete process group."""

    def __init__(
        self,
        *,
        command_template: str,
        cwd: Path,
        target_url_template: str = "http://127.0.0.1:{port}",
        readiness_path: str = "/",
        startup_timeout_seconds: float = 60.0,
        environment: dict[str, str] | None = None,
    ):
        self.command_template = command_template
        self.cwd = cwd
        self.target_url_template = target_url_template
        self.readiness_path = readiness_path
        self.startup_timeout_seconds = startup_timeout_seconds
        self.environment = self._validated_environment(environment or {})
        self.port = self._available_port()
        self.command = command_template.format(port=self.port, host="127.0.0.1")
        self.target_url = target_url_template.format(port=self.port, host="127.0.0.1")
        self._process: subprocess.Popen[str] | None = None
        # The session owns these streams across enter/exit so evidence remains readable.
        self._stdout = tempfile.TemporaryFile(  # noqa: SIM115
            mode="w+", encoding="utf-8"
        )
        self._stderr = tempfile.TemporaryFile(  # noqa: SIM115
            mode="w+", encoding="utf-8"
        )
        self._ready = False
        self._cleanup: tuple[bool, bool] = (False, False)
        self._pid: int | None = None

    def __enter__(self) -> LocalPreviewSession:
        # A login shell would put the operator's profile load inside the
        # readiness budget: unbounded, machine-specific, and charged against a
        # timeout that is meant to measure the server. Resolve the project's
        # pinned Node explicitly instead, the way every other spawned worktree
        # command already does.
        env = project_environment(self.cwd, {**self.environment, "PORT": str(self.port)})
        try:
            self._process = subprocess.Popen(
                ["/bin/zsh", "-c", self.command],
                cwd=self.cwd,
                env=env,
                stdout=self._stdout,
                stderr=self._stderr,
                text=True,
                start_new_session=True,
            )
            self._pid = self._process.pid
            self._wait_until_ready()
            self._ready = True
            return self
        except Exception as exc:
            self.close()
            raise PreviewStartError(
                f"Local preview did not become ready: {_bounded_message(exc)}",
                self.build_process_evidence(),
            ) from exc

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                with suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)
        direct_reaped = process is None or process.poll() is not None
        group_reaped = self._group_reaped(self._pid)
        self._cleanup = (direct_reaped, group_reaped)
        self._process = process

    def build_process_evidence(self) -> PreviewProcessEvidence:
        process = self._process
        return PreviewProcessEvidence(
            command=self.command,
            cwd=str(self.cwd),
            target_url=self.target_url,
            pid=self._pid,
            ready=self._ready,
            returncode=process.poll() if process is not None else None,
            direct_child_reaped=self._cleanup[0],
            process_group_reaped=self._cleanup[1],
            stdout_tail=self._stream_tail(self._stdout),
            stderr_tail=self._stream_tail(self._stderr),
        )

    def _wait_until_ready(self) -> None:
        assert self._process is not None
        deadline = time.monotonic() + self.startup_timeout_seconds
        readiness_url = urljoin(self.target_url + "/", self.readiness_path.lstrip("/"))
        last_error = "no response"
        while time.monotonic() < deadline:
            returncode = self._process.poll()
            if returncode is not None:
                raise RuntimeError(f"preview process exited with {returncode}")
            try:
                response = httpx.get(readiness_url, timeout=1.0, follow_redirects=False)
                if response.status_code < 400:
                    return
                last_error = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            time.sleep(0.1)
        raise TimeoutError(
            f"preview readiness timed out after {self.startup_timeout_seconds:.0f}s ({last_error})"
        )

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def _group_reaped(pgid: int | None) -> bool:
        if pgid is None:
            return True
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    @staticmethod
    def _stream_tail(stream: Any, limit: int = 4000) -> str:
        stream.flush()
        stream.seek(0)
        return stream.read()[-limit:]

    @staticmethod
    def _validated_environment(environment: dict[str, str]) -> dict[str, str]:
        credential_suffixes = (
            "_TOKEN",
            "_SECRET",
            "_PASSWORD",
            "_CREDENTIAL",
            "_API_KEY",
            "_ACCESS_KEY",
            "_PRIVATE_KEY",
        )
        for key in environment:
            normalized = key.strip().upper()
            if normalized.endswith(credential_suffixes) or normalized in {
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "CREDENTIAL",
                "API_KEY",
            }:
                raise ValueError(f"local preview environment rejects credential-shaped key: {key}")
        return dict(environment)


DEFAULT_RESPONSIVE_VIEWPORTS = (
    BrowserViewport(name="mobile", width=375, height=812),
    BrowserViewport(name="desktop", width=1440, height=1000),
)


__all__ = [
    "BrowserAcceptanceEvidence",
    "BrowserAcceptanceArtifactWriter",
    "BrowserAcceptanceCancellation",
    "BrowserAcceptanceRun",
    "BrowserAcceptanceRunner",
    "BrowserAcceptanceRequest",
    "BrowserAcceptanceStatus",
    "BrowserCaptureEvidence",
    "BrowserViewport",
    "DEFAULT_RESPONSIVE_VIEWPORTS",
    "LocalPreviewSession",
    "PreviewProcessEvidence",
    "PreviewStartError",
]
