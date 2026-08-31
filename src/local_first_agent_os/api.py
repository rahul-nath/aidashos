# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any, assert_never

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import TypeAdapter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .access_posture import announce_posture
from .contracts import (
    FileIngressRequest,
    IngressEvent,
    PiDirectiveRequest,
    QuestionRequest,
    SourceType,
    WorkflowType,
    WorkflowyWriteRequest,
    WorkspaceId,
    WorkUnitDecisionRequest,
)
from .dbos_app import (
    DbosLaunchFailed,
    DbosUnavailable,
    dbos_runtime_active,
    launch_dbos,
    run_workflow_durably,
    shutdown_dbos,
    start_durable_workflow,
)
from .gawd_walkthru import GawdWalkthruStore, WalkthruError
from .gawd_walkthru_runtime import GawdWalkthruSummarizer
from .ingress import (
    BoundsError,
    normalize_file_event,
    normalize_prompt_event,
    normalize_scheduled_event,
)
from .observability import instrument_fastapi_app
from .operator_commands import (
    CancelWorkUnit,
    IntegrationTriggered,
    OperatorExecutionContext,
    ResolveWorkUnitDecision,
    ResumeWorkUnit,
    TriggerIntegration,
    WorkUnitCancelled,
    WorkUnitDecisionExecuted,
    WorkUnitResumed,
    execute_operator_command,
)
from .operator_identity import OperatorIdentityRefused, verify_operator_actor
from .project_action import ProjectActionSnapshot, build_project_action_snapshot
from .project_activity import (
    DEFAULT_ACTIVITY_LIMIT,
    MAX_ACTIVITY_LIMIT,
    ActivityCursor,
    ProjectActivityPage,
    build_project_activity_page,
)
from .project_center import ProjectCenterView, load_project_center
from .refinery.loop import run_refinery
from .refinery.trigger import (
    IntegrationTriggerResult,
    plan_integration_trigger,
)
from .runtime import get_runtime
from .settings import get_settings
from .work_units import repository as work_unit_repo
from .work_units import service as work_units
from .work_units.authoring import (
    AcceptWalkthru,
    AnswerWalkthru,
    CompileDesignDocRequest,
    CompileDesignDocResponse,
    DeliveryContractView,
    EditWalkthru,
    FinishWalkthru,
    PermissionPolicyView,
    ReviseWalkthru,
    SkipWalkthru,
    StartWalkthruRequest,
    StartWorkUnitRequest,
    StartWorkUnitResponse,
    WalkthruTransition,
    WalkthruView,
)
from .work_units.next_commands import NextCommandSet, next_commands_for_view
from .work_units.projection import (
    ArtifactView,
    EventView,
    WorkUnitArtifactList,
    WorkUnitCancelResult,
    WorkUnitDecisionResult,
    WorkUnitEventPage,
    WorkUnitIndex,
    WorkUnitResumeResult,
    WorkUnitSummary,
    WorkUnitView,
)
from .work_units.repository import DecisionRequestMismatch, WorkUnitError
from .work_units.root_workflow import EnqueueDelivery
from .work_units.status_legend import STATUS_LEGEND, StatusLegendView
from .workflow import run_workflow
from .workflow.saga_support import resolve_project_repo_root

WORKFLOWS_TOTAL = Counter("workflow_runs_total", "Workflow runs", ["workflow_type", "status"])
MANUAL_REVIEW_DEPTH = Gauge(
    "manual_review_queue_depth",
    "Manual review queue depth",
    ["workflow_type"],
)
logger = logging.getLogger(__name__)


def _drain_accepted_integration(target_project_id: str) -> None:
    """Run one durable queue drain without turning an HTTP response into a job lease."""

    try:
        run_refinery(target_project_id, max_polls=1)
    except Exception:
        # The queue row remains Queued or recoverable InFlight. The next click or
        # resident refinery can retry it, while the exception stays visible.
        logger.exception("cockpit integration trigger failed for %s", target_project_id)


def create_app() -> FastAPI:
    settings = get_settings()
    runtime = get_runtime()
    walkthru_store = GawdWalkthruStore(resolve_project_repo_root())
    walkthru_adapter = TypeAdapter(WalkthruView)

    def run_workflow_result(workflow_type: WorkflowType, event: IngressEvent) -> dict[str, Any]:
        if settings.use_dbos:
            return run_workflow_durably(workflow_type, event)
        return run_workflow(workflow_type, event).model_dump(mode="json")

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # DBOS owns recovery of DBOS workflow executions. Recovery of legacy
        # application workflow rows is an explicit operator action through
        # `local-agent resume-workflows`; it must never delay HTTP readiness.
        outcome = launch_dbos()
        if isinstance(outcome, DbosUnavailable | DbosLaunchFailed):
            # This process delivers, resumes, and recovers through the runtime
            # it just failed to launch; serving anyway is a refusal presenting
            # as a success.
            raise RuntimeError(
                "the durable runtime (DBOS) is configured on and did not launch: "
                f"{outcome.reason}. Refusing to serve with delivery, resume, and "
                "recovery unavailable."
            )
        announce_posture(settings.access_posture)
        try:
            yield
        finally:
            # The runtime's worker threads are non-daemon; without this a
            # Ctrl-C'd server prints its shutdown banner and then hangs in
            # interpreter finalization joining them.
            shutdown_dbos()

    app = FastAPI(
        title="Local-First Agent OS",
        version=settings.application_version,
        lifespan=lifespan,
    )
    instrument_fastapi_app(app, settings)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health(response: Response) -> dict[str, Any]:
        # Probed, not described. This endpoint used to report the database it was
        # *configured* with and call that ok, so it answered ok throughout an
        # outage where every ledger query timed out - including to the operator
        # watching the cockpit, and to a scripted wait that took the green as
        # proof the stack was back. A check that cannot fail is not a check, and
        # its failure mode is worse than having none: it is trusted.
        #
        # `SELECT 1` rather than a table read, because what this answers is
        # whether the ledger is reachable, not whether it holds anything. A
        # schema question deserves its own answer and its own name.
        ledger_error: str | None = None
        try:
            with runtime.database.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            ledger_error = f"{type(exc).__name__}: {exc}"
        if ledger_error is not None:
            # 503 so a probe that reads the status line rather than the body -
            # `curl -f`, a Compose healthcheck, a Kubernetes probe - is told the
            # same thing the body says.
            response.status_code = 503
        return {
            "status": "ok" if ledger_error is None else "degraded",
            "ledger": {"reachable": ledger_error is None, "error": ledger_error},
            "app": settings.app_name,
            "use_dbos": settings.use_dbos,
            # Distinct from `use_dbos`: that is configuration, this is whether a
            # launched runtime is live in this process right now.
            "dbos_launched": dbos_runtime_active(),
            "use_conductor": bool(settings.dbos_conductor_key),
            "mock_models": settings.mock_models,
            # The database this process is pointed at, never the URL that
            # reaches it. This endpoint is unauthenticated because the Compose
            # healthcheck and the Kubernetes probes cannot present a credential,
            # so whatever it reports is readable by whatever can reach the port.
            "database": settings.database_identity,
            # Reported because the failure mode of a permissive posture is
            # forgetting it is on. An operator should be able to find out
            # without reading the environment of a process they did not start.
            "access_posture": settings.access_posture.value,
        }

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        summary = runtime.repository.dashboard_summary()
        MANUAL_REVIEW_DEPTH.labels(workflow_type="all").set(summary["manual_review_queue_depth"])
        return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)

    @app.get("/dashboard")
    def dashboard() -> dict[str, Any]:
        return runtime.repository.dashboard_summary()

    @app.get("/projects", response_model=ProjectCenterView)
    def projects() -> ProjectCenterView:
        # Validated at the boundary rather than returned as a dict, which is what
        # lets the web client's project types be generated instead of hand-written.
        return ProjectCenterView.model_validate(
            load_project_center(settings).as_dict(include_git=True)
        )

    @app.get("/projects/{project_id}/action", response_model=ProjectActionSnapshot)
    def project_action(project_id: str) -> ProjectActionSnapshot:
        try:
            snapshot = build_project_action_snapshot(project_id, settings=settings)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "schema_version": "project_action_snapshot_error.v1",
                    "project_id": project_id,
                    "reason": str(exc),
                },
            ) from exc
        return snapshot

    @app.get(
        "/approvals/{approval_id}/integration",
        response_model=IntegrationTriggerResult,
    )
    def integration_status(approval_id: str) -> IntegrationTriggerResult:
        return plan_integration_trigger(approval_id)

    @app.post(
        "/approvals/{approval_id}/integration",
        response_model=IntegrationTriggerResult,
    )
    def trigger_integration(
        approval_id: str,
        background_tasks: BackgroundTasks,
    ) -> IntegrationTriggerResult:
        try:
            executed = execute_operator_command(
                TriggerIntegration(
                    approval_id=approval_id,
                    actor=verify_operator_actor("api_operator"),
                ),
                context=OperatorExecutionContext(
                    settings=settings,
                    submit_integration=lambda target_project_id: background_tasks.add_task(
                        _drain_accepted_integration,
                        target_project_id,
                    ),
                    plan_integration=plan_integration_trigger,
                ),
            )
        except OperatorIdentityRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not isinstance(executed, IntegrationTriggered):
            raise AssertionError(f"TriggerIntegration returned {type(executed).__name__}")
        return executed.result

    @app.get("/projects/{project_id}/activity", response_model=ProjectActivityPage)
    def project_activity(
        project_id: str,
        lease_id: str | None = None,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=DEFAULT_ACTIVITY_LIMIT, ge=1, le=MAX_ACTIVITY_LIMIT),
    ) -> ProjectActivityPage:
        # A sequence only means something inside the lease it counts within, so
        # a caller without a lease has no position to resume from.
        cursor = (
            ActivityCursor(lease_id=lease_id, after_sequence=after_sequence) if lease_id else None
        )
        try:
            page = build_project_activity_page(
                project_id,
                cursor=cursor,
                limit=limit,
                settings=settings,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "schema_version": "project_activity_page_error.v1",
                    "project_id": project_id,
                    "reason": str(exc),
                },
            ) from exc
        return page

    # WorkUnit routes. These read and write durable lifecycle state; there is
    # deliberately no endpoint that sets a phase or a milestone status, because
    # every legal change is already the consequence of a fact the engine records.

    @app.post("/authoring/walkthroughs", response_model=WalkthruView)
    def start_walkthru(req: StartWalkthruRequest) -> WalkthruView:
        if req.target_project_id is not None:
            try:
                load_project_center(settings).project_by_id(req.target_project_id)
            except KeyError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        payload = walkthru_store.start(
            target_project_id=req.target_project_id,
            create_target_id=req.create_target_id,
            operation_id=req.operation_id,
        )
        return walkthru_adapter.validate_python(payload)

    @app.get("/authoring/walkthroughs/{walkthru_id}", response_model=WalkthruView)
    def read_walkthru(walkthru_id: str) -> WalkthruView:
        try:
            payload = walkthru_store.read_status(walkthru_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return walkthru_adapter.validate_python(payload)

    @app.post(
        "/authoring/walkthroughs/{walkthru_id}/transitions",
        response_model=WalkthruView,
    )
    def transition_walkthru(
        walkthru_id: str,
        req: WalkthruTransition,
    ) -> WalkthruView:
        try:
            match req:
                case AnswerWalkthru():
                    payload = walkthru_store.answer(
                        walkthru_id,
                        req.verbatim,
                        operation_id=req.operation_id,
                        summarize=GawdWalkthruSummarizer(
                            runtime,
                            f"gawd-walkthru:{walkthru_id}:{req.operation_id}",
                        ),
                    )
                case AcceptWalkthru():
                    payload = walkthru_store.accept_proposed_summary(
                        walkthru_id,
                        operation_id=req.operation_id,
                    )
                case ReviseWalkthru():
                    payload = walkthru_store.revise_proposed_summary(
                        walkthru_id,
                        req.accepted_summary,
                        operation_id=req.operation_id,
                    )
                case SkipWalkthru():
                    payload = walkthru_store.skip_section(
                        walkthru_id,
                        operation_id=req.operation_id,
                    )
                case EditWalkthru():
                    payload = walkthru_store.edit_accepted_summary(
                        walkthru_id,
                        req.section_id,
                        req.accepted_summary,
                        operation_id=req.operation_id,
                    )
                case FinishWalkthru():
                    payload = walkthru_store.write_completed_sparse_gawd_draft(
                        walkthru_id,
                        operation_id=req.operation_id,
                    )
                case _ as unreachable:
                    assert_never(unreachable)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WalkthruError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return walkthru_adapter.validate_python(payload)

    @app.post("/authoring/design-docs/compile", response_model=CompileDesignDocResponse)
    def compile_design_doc(req: CompileDesignDocRequest) -> CompileDesignDocResponse:
        result = work_units.compile_design_doc_text(
            req.raw_content,
            design_doc_id=req.design_doc_id,
            source_path=req.source_path,
        )
        permission_policy = None
        delivery_contract = None
        if result.compiled_plan_revision_id is not None:
            plan = work_unit_repo.get_compiled_plan_revision(result.compiled_plan_revision_id).plan
            if plan.permission_policy is not None:
                permission_policy = PermissionPolicyView.from_policy(plan.permission_policy)
            delivery_payload = plan.delivery_contract.to_payload()
            delivery_contract = DeliveryContractView(
                kind=str(delivery_payload["kind"]),
                artifact_types=tuple(delivery_payload.get("artifact_types", ())),
                reason=(
                    str(delivery_payload["reason"])
                    if delivery_payload.get("reason") is not None
                    else None
                ),
            )
        return CompileDesignDocResponse(
            **result.to_payload(),
            permission_policy=permission_policy,
            delivery_contract=delivery_contract,
        )

    @app.post("/work-units", response_model=StartWorkUnitResponse)
    def start_work_unit(req: StartWorkUnitRequest) -> StartWorkUnitResponse:
        try:
            started = work_units.start_work_unit(
                req.compiled_plan_revision_id,
                title=req.title,
                approved_plan_hash=req.approved_plan_hash,
            )
        except WorkUnitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StartWorkUnitResponse.model_validate(started)

    @app.get("/status-legend", response_model=StatusLegendView)
    def status_legend() -> StatusLegendView:
        """Meaning and operator next-move for every status token the cockpit renders.

        A constant, and served anyway: the legend must describe the statuses this
        server emits, and a copy checked into the web bundle would drift from
        them at the first mismatched deploy.
        """

        return STATUS_LEGEND

    @app.get("/work-units", response_model=WorkUnitIndex)
    def work_unit_index(status: str | None = None) -> WorkUnitIndex:
        try:
            return WorkUnitIndex(
                work_units=[
                    WorkUnitSummary.model_validate(item)
                    for item in work_units.list_work_units(status)
                ]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/work-units/{work_unit_id}", response_model=WorkUnitView)
    def work_unit_detail(work_unit_id: str) -> WorkUnitView:
        try:
            return work_units.get_work_unit(work_unit_id)
        except WorkUnitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/work-units/{work_unit_id}/next-commands", response_model=NextCommandSet)
    def work_unit_next_commands(work_unit_id: str) -> NextCommandSet:
        """What an operator does next, derived rather than written twice.

        The terminal has printed this block since the next-command affordance
        shipped; the cockpit had no equivalent, so an operator reading a BLOCKED
        pill in the browser was told the work stopped and not what to do about
        it. `next_commands_for_view` is a pure function of the same view this
        API already serves, so exposing it costs one route and keeps one rule
        table answering both surfaces.
        """

        try:
            view = work_units.get_work_unit(work_unit_id)
        except WorkUnitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return next_commands_for_view(view)

    @app.get("/work-units/{work_unit_id}/events", response_model=WorkUnitEventPage)
    def work_unit_events(
        work_unit_id: str,
        after_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> WorkUnitEventPage:
        try:
            events = work_units.list_work_unit_events(
                work_unit_id,
                after_sequence=after_sequence,
                limit=limit,
            )
        except WorkUnitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return WorkUnitEventPage(
            work_unit_id=work_unit_id,
            events=[EventView.model_validate(item) for item in events],
        )

    @app.get("/work-units/{work_unit_id}/artifacts", response_model=WorkUnitArtifactList)
    def work_unit_artifacts(work_unit_id: str) -> WorkUnitArtifactList:
        try:
            artifacts = work_units.list_work_unit_artifacts(work_unit_id)
        except WorkUnitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return WorkUnitArtifactList(
            work_unit_id=work_unit_id,
            artifacts=[ArtifactView.model_validate(item) for item in artifacts],
        )

    @app.post("/work-units/{work_unit_id}/decisions", response_model=WorkUnitDecisionResult)
    def work_unit_decision(
        work_unit_id: str,
        req: WorkUnitDecisionRequest,
    ) -> WorkUnitDecisionResult:
        try:
            executed = execute_operator_command(
                ResolveWorkUnitDecision(
                    work_unit_id=work_unit_id,
                    request_id=req.request_id,
                    decision=req.decision.value,
                    idempotency_key=req.idempotency_key,
                    actor=verify_operator_actor(req.decided_by),
                    payload=req.payload,
                ),
                context=OperatorExecutionContext(settings=settings),
            )
            if not isinstance(executed, WorkUnitDecisionExecuted):
                raise AssertionError(f"ResolveWorkUnitDecision returned {type(executed).__name__}")
        except DecisionRequestMismatch as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OperatorIdentityRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except WorkUnitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return WorkUnitDecisionResult.model_validate(executed.payload)

    @app.post("/work-units/{work_unit_id}/cancel", response_model=WorkUnitCancelResult)
    def work_unit_cancel(work_unit_id: str) -> WorkUnitCancelResult:
        try:
            executed = execute_operator_command(
                CancelWorkUnit(
                    work_unit_id=work_unit_id,
                    reason="cancelled by operator",
                    actor=verify_operator_actor("api_operator"),
                ),
                context=OperatorExecutionContext(settings=settings),
            )
            if not isinstance(executed, WorkUnitCancelled):
                raise AssertionError(f"CancelWorkUnit returned {type(executed).__name__}")
        except OperatorIdentityRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except WorkUnitError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return WorkUnitCancelResult.model_validate(executed.payload)

    @app.post("/work-units/{work_unit_id}/resume", response_model=WorkUnitResumeResult)
    def work_unit_resume(work_unit_id: str) -> WorkUnitResumeResult:
        try:
            executed = execute_operator_command(
                ResumeWorkUnit(
                    work_unit_id=work_unit_id,
                    delivery=EnqueueDelivery.DURABLE,
                    actor=verify_operator_actor("api_operator"),
                ),
                context=OperatorExecutionContext(settings=settings),
            )
            if not isinstance(executed, WorkUnitResumed):
                raise AssertionError(f"ResumeWorkUnit returned {type(executed).__name__}")
        except OperatorIdentityRefused as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except WorkUnitError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return WorkUnitResumeResult.model_validate(executed.payload)

    @app.get("/workflows")
    def workflows(limit: int = 50) -> dict[str, Any]:
        return {"workflows": runtime.repository.list_workflows(limit=limit)}

    @app.get("/registry/models")
    def model_registry() -> dict[str, Any]:
        return runtime.model_registry.as_dict()

    @app.get("/registry/workspaces")
    def workspaces() -> dict[str, Any]:
        return {
            "workspaces": [policy.model_dump(mode="json") for policy in runtime.policy_store.all()]
        }

    @app.post("/questions")
    def question(req: QuestionRequest) -> dict[str, Any]:
        event = normalize_prompt_event(req.prompt, workspace_id=req.workspace_id)
        result = (
            start_durable_workflow(WorkflowType.GENERAL_QUESTIONS, event)
            if req.use_dbos
            else run_workflow_result(WorkflowType.GENERAL_QUESTIONS, event)
        )
        if isinstance(result, str):
            return {"workflow_id": result, "started": True}
        WORKFLOWS_TOTAL.labels(
            workflow_type=result["workflow_type"],
            status=result["status"],
        ).inc()
        return result

    @app.post("/ingress/file")
    def ingress_file(req: FileIngressRequest) -> dict[str, Any]:
        try:
            event = normalize_file_event(
                path=Path(req.path),
                workspace_id=req.workspace_id,
                workflow_type=req.workflow_type,
                event_type=req.event_type,
                stable=req.stable,
            )
        except BoundsError as exc:
            raise HTTPException(status_code=422, detail={"reason": exc.reason}) from exc
        if req.use_dbos:
            return {
                "workflow_id": start_durable_workflow(req.workflow_type, event),
                "event": event.model_dump(mode="json"),
            }
        return run_workflow_result(req.workflow_type, event)

    @app.post("/apple-notes/sync")
    def apple_notes_sync(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = normalize_scheduled_event(
            source_type=SourceType.APPLE_NOTES,
            workspace_id=WorkspaceId.APPLE_NOTES.value,
            event_type="notes.poll",
            payload=payload or {},
        )
        return run_workflow_result(WorkflowType.APPLE_NOTES_SYNC, event)

    @app.post("/workflowy/sync")
    def workflowy_sync(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        event = normalize_scheduled_event(
            source_type=SourceType.WORKFLOWY,
            workspace_id=WorkspaceId.WORKFLOWY.value,
            event_type="workflowy.poll",
            payload=payload or {},
        )
        return run_workflow_result(WorkflowType.WORKFLOWY_SYNC, event)

    @app.post("/workflowy/write")
    def workflowy_write(req: WorkflowyWriteRequest) -> dict[str, Any]:
        event = normalize_scheduled_event(
            source_type=SourceType.WORKFLOWY,
            workspace_id=req.workspace_id,
            event_type="workflowy.write_request",
            payload={"parent_node_id": req.parent_node_id, "content": req.content},
        )
        if req.use_dbos:
            return {"workflow_id": start_durable_workflow(WorkflowType.WORKFLOWY_WRITE, event)}
        return run_workflow_result(WorkflowType.WORKFLOWY_WRITE, event)

    @app.get("/retrieval/search")
    def retrieval_search(
        query: str,
        workspace_id: str | None = None,
        top_k: int = 10,
    ) -> dict[str, Any]:
        hits = runtime.retrieval.search(query, workspace_id=workspace_id, top_k=top_k)
        return {"hits": [hit.__dict__ for hit in hits]}

    @app.post("/pi/directive")
    def pi_directive(req: PiDirectiveRequest) -> dict[str, Any]:
        from .pi_channel import run_terminal_query

        return {
            "results": [
                item
                for item in run_terminal_query(
                    req.text,
                    workspace_id=req.workspace_id,
                    shell_session_id=req.session_id or f"api-{req.workspace_id}",
                    context=req.context,
                    max_window_tokens=req.max_window_tokens,
                    streaming=False,
                )
                if isinstance(item, dict)
            ]
        }

    @app.post("/pi/directive/stream")
    def pi_directive_stream(req: PiDirectiveRequest) -> StreamingResponse:
        from .pi_channel import run_terminal_query

        events: Queue[dict[str, Any] | None] = Queue()

        def emit(event: dict[str, Any]) -> None:
            events.put(event)

        def worker() -> None:
            try:
                result_dicts: list[dict[str, Any]] = []
                for item in run_terminal_query(
                    req.text,
                    workspace_id=req.workspace_id,
                    shell_session_id=req.session_id or f"api-{req.workspace_id}",
                    context=req.context,
                    max_window_tokens=req.max_window_tokens,
                    streaming=True,
                ):
                    if isinstance(item, str):
                        emit({"type": "delta", "text": item})
                    else:
                        result_dicts.append(item)
                emit({"type": "result", "results": result_dicts})
                emit({"type": "done"})
            except Exception as exc:
                emit({"type": "error", "error": str(exc)})
            finally:
                events.put(None)

        def event_stream():
            while True:
                event = events.get()
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        Thread(target=worker, daemon=True).start()
        return StreamingResponse(event_stream(), media_type="text/event-stream")

    if settings.web_dist and settings.web_dist.exists():
        web_dist = settings.web_dist.resolve()
        assets_dir = web_dist / "assets"
        if assets_dir.exists():
            app.mount("/assets", StaticFiles(directory=assets_dir), name="web-assets")

        @app.get("/", include_in_schema=False)
        def web_index() -> FileResponse:
            return FileResponse(web_dist / "index.html")

        @app.get("/{path:path}", include_in_schema=False)
        def web_fallback(path: str) -> FileResponse:
            candidate = (web_dist / path).resolve()
            try:
                candidate.relative_to(web_dist)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="Not found") from exc
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")

    return app
