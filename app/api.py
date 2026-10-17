import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from starlette.middleware.base import RequestResponseEndpoint

from app.config import Settings, get_settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.persistence.postgres.migrate import MIGRATIONS
from app.runtime import Runtime
from app.security.auth import verify_bearer_token

logger = logging.getLogger(__name__)
_bearer = HTTPBearer(auto_error=False, scheme_name="TripMateBearer")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateTripBody(RequestModel):
    destination: str = Field(min_length=1, max_length=120)
    start_date: date
    end_date: date
    timezone: str = Field(min_length=1, max_length=64)
    party_size: int = Field(ge=1, le=100)
    budget_limit: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    currency: str | None = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    companion_type: Literal["solo", "couple", "family", "elderly", "mixed", "other"] | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def valid_trip_window(self) -> "CreateTripBody":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class CreateRunBody(RequestModel):
    message: str = Field(min_length=1, max_length=4000)
    trip_id: UUID | None = None


class TripConfirmBody(RequestModel):
    expected_version: int | None = Field(default=None, ge=1)


class ActionConfirmationBody(RequestModel):
    expected_confirmation_version: int = Field(ge=1)


class ActionRejectBody(RequestModel):
    expected_version: int = Field(ge=1)


class ConfirmPreferenceActionBody(RequestModel):
    confirmation_token: str = Field(min_length=20, max_length=500)


class HoldReservationBody(RequestModel):
    action_id: UUID
    slot_id: UUID
    quantity: int = Field(ge=1, le=100)
    expected_inventory_version: int | None = Field(default=None, ge=1)
    expected_activity_version: int | None = Field(default=None, ge=1)
    confirmation_token: str = Field(min_length=20, max_length=500)


class ConfirmReservationBody(RequestModel):
    action_id: UUID
    confirmation_token: str = Field(min_length=20, max_length=500)
    expected_version: int = Field(ge=1)


class CancelReservationBody(RequestModel):
    action_id: UUID
    confirmation_token: str = Field(min_length=20, max_length=500)
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=200)


class PreferenceBody(RequestModel):
    scope: str = Field(min_length=1, max_length=80)
    key: str = Field(min_length=1, max_length=120)
    value: Any
    expected_version: int | None = Field(default=None, ge=1)


class PreferenceDeleteBody(RequestModel):
    expected_version: int = Field(ge=1)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    runtime = await Runtime.create(app.state.settings)
    app.state.runtime = runtime
    try:
        yield
    finally:
        await runtime.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    app = FastAPI(
        title="TripMate API",
        version="0.1.0",
        description="Weekend itinerary planning and PostgreSQL-authoritative reservations.",
        lifespan=_lifespan,
    )
    app.state.settings = config

    @app.middleware("http")
    async def request_context_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        supplied_id = request.headers.get("X-Request-Id", "")
        request_id = supplied_id if _REQUEST_ID_RE.fullmatch(supplied_id) else str(uuid4())
        request.state.request_id = request_id
        request.state.trace_id = request_id.replace("-", "")
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > config.max_request_body_bytes:
                    return _error_response(
                        request,
                        DomainError(
                            "REQUEST_TOO_LARGE", "Request body exceeds the allowed size", 413
                        ),
                    )
            except ValueError:
                return _error_response(
                    request, DomainError("INVALID_ARGUMENT", "Content-Length is invalid", 400)
                )
        with trace.get_tracer("tripmate.http").start_as_current_span(
            f"{request.method} {request.url.path}"
        ) as span:
            span_context = trace.get_current_span().get_span_context()
            if span_context.is_valid:
                request.state.trace_id = f"{span_context.trace_id:032x}"
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("http.route", request.url.path)
            span.set_attribute("tripmate.request_id", request_id)
            try:
                response = await call_next(request)
            except DomainError as exc:
                response = _error_response(request, exc)
            span.set_attribute("http.response.status_code", response.status_code)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return _error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {"field": ".".join(str(part) for part in error["loc"]), "message": error["msg"]}
            for error in exc.errors()
        ]
        return _error_response(
            request,
            DomainError(
                "VALIDATION_FAILED", "Request validation failed", 422, details={"fields": fields}
            ),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return _error_response(request, DomainError("HTTP_ERROR", str(exc.detail), exc.status_code))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_request_error",
            extra={
                "request_id": getattr(request.state, "request_id", "unknown"),
                "error_type": type(exc).__name__,
            },
        )
        return _error_response(
            request,
            DomainError("INTERNAL_ERROR", "An unexpected error occurred", 500),
        )

    async def context_dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> RequestContext:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise DomainError("UNAUTHORIZED", "A valid bearer token is required", 401)
        claims = verify_bearer_token(
            credentials.credentials, config.auth_signing_secret.get_secret_value()
        )
        runtime: Runtime = request.app.state.runtime
        async with runtime.sessions() as session:
            active = await session.scalar(
                text("""SELECT EXISTS(SELECT 1 FROM tripmate.tenant t
                    JOIN tripmate.user_profile u ON u.tenant_id=t.tenant_id
                    WHERE t.tenant_id=:tenant AND u.user_id=:user
                      AND t.status='ACTIVE' AND u.status='ACTIVE')"""),
                {"tenant": claims["tenant_id"], "user": claims["user_id"]},
            )
        if not active:
            raise DomainError("UNAUTHORIZED", "Bearer token principal is inactive", 401)
        span_context = trace.get_current_span().get_span_context()
        trace_id = (
            f"{span_context.trace_id:032x}" if span_context.is_valid else request.state.trace_id
        )
        return RequestContext(
            request_id=request.state.request_id,
            trace_id=trace_id,
            tenant_id=claims["tenant_id"],
            user_id=claims["user_id"],
            roles=("user",),
            deadline=datetime.now(UTC) + timedelta(minutes=2),
            source="http",
        )

    def envelope(request: Request, data: Any) -> dict[str, Any]:
        return {
            "request_id": request.state.request_id,
            "trace_id": request.state.trace_id,
            "data": jsonable_encoder(data),
        }

    def service(request: Request) -> Runtime:
        return cast(Runtime, request.app.state.runtime)

    @app.get("/health/live", tags=["health"])
    async def health_live(request: Request) -> dict[str, Any]:
        return envelope(request, {"status": "UP"})

    @app.get("/health/ready", tags=["health"])
    async def health_ready(request: Request, response: Response) -> dict[str, Any]:
        runtime = service(request)
        checks: dict[str, str] = {}
        try:
            async with runtime.sessions() as session:
                await session.execute(text("SELECT 1 FROM tripmate.agent_run LIMIT 1"))
                expected_migration = sorted(path.name for path in MIGRATIONS.glob("V*__*.sql"))[-1]
                applied_migration = await session.scalar(
                    text("""SELECT version FROM public.tripmate_migration_history
                        ORDER BY version DESC LIMIT 1"""),
                )
                checks["migrations"] = "UP" if applied_migration == expected_migration else "DOWN"
                worker = await session.scalar(
                    text("""SELECT EXISTS(SELECT 1 FROM tripmate.worker_heartbeat
                        WHERE worker_name='agent-worker' AND heartbeat_at>now()-interval '45 seconds')""")
                )
                rag_alias = await session.scalar(
                    text("""SELECT EXISTS(
                        SELECT 1 FROM tripmate.knowledge_active_alias a
                        JOIN tripmate.knowledge_manifest m
                          ON m.tenant_id=a.tenant_id AND m.manifest_id=a.manifest_id
                        WHERE a.index_alias=:alias AND m.status='ACTIVE')"""),
                    {"alias": runtime.settings.rag_index_alias},
                )
            checks["postgres"] = "UP"
            checks["agent_worker"] = "UP" if worker else "DOWN"
            checks["rag_alias"] = "UP" if rag_alias else "DOWN"
        except Exception:
            checks["postgres"] = "DOWN"
            checks["agent_worker"] = "UNKNOWN"
            checks["migrations"] = "UNKNOWN"
            checks["rag_alias"] = "UNKNOWN"
        try:
            checks["redis"] = "UP" if await runtime.redis.ping() else "DOWN"
            checks["redis_scripts"] = (
                "UP" if runtime.projection.project_snapshot_script is not None else "DOWN"
            )
            checkpoints = getattr(runtime.checkpointer, "checkpoints_index", None)
            writes = getattr(runtime.checkpointer, "checkpoint_writes_index", None)
            checks["checkpoint_saver"] = (
                "UP"
                if checkpoints is not None
                and writes is not None
                and await checkpoints.exists()
                and await writes.exists()
                else "DOWN"
            )
            checks["rag_vector_index"] = (
                "UP"
                if runtime.knowledge.vector_index is not None
                and await runtime.knowledge.vector_index.check_ready()
                else "DOWN"
            )
        except Exception:
            checks["redis"] = "DOWN"
            checks["redis_scripts"] = "DOWN"
            checks["checkpoint_saver"] = "DOWN"
            checks["rag_vector_index"] = "DOWN"
        ready = all(value == "UP" for value in checks.values())
        if not ready:
            response.status_code = 503
        return envelope(request, {"status": "UP" if ready else "DOWN", "checks": checks})

    @app.get("/v1/operations/faults", tags=["operations"])
    async def operations_faults(
        request: Request, ctx: RequestContext = Depends(context_dependency)
    ) -> dict[str, Any]:
        runtime = service(request)
        async with runtime.sessions() as session:
            outbox_rows = await session.execute(
                text("""SELECT status,count(*) AS count,
                    EXTRACT(epoch FROM now()-MIN(created_at)) AS oldest_age_seconds
                    FROM tripmate.outbox_event WHERE tenant_id=:tenant AND status IN ('PENDING','FAILED')
                    GROUP BY status ORDER BY status"""),
                {"tenant": ctx.tenant_id},
            )
            inbox_rows = await session.execute(
                text("""SELECT status,count(*) AS count,
                    count(*) FILTER (WHERE status='PROCESSING' AND lease_until<=now()) AS expired_leases
                    FROM tripmate.inbox_event WHERE tenant_id=:tenant GROUP BY status ORDER BY status"""),
                {"tenant": ctx.tenant_id},
            )
            dead_letters = await session.execute(
                text("""SELECT error_code,count(*) AS count FROM tripmate.dead_letter_event
                    WHERE tenant_id=:tenant AND status='OPEN' GROUP BY error_code ORDER BY error_code"""),
                {"tenant": ctx.tenant_id},
            )
            failed_runs = await session.execute(
                text("""SELECT last_error_code,count(*) AS count FROM tripmate.agent_run
                    WHERE tenant_id=:tenant AND status='FAILED' GROUP BY last_error_code
                    ORDER BY last_error_code"""),
                {"tenant": ctx.tenant_id},
            )
            reminder_rows = await session.execute(
                text("""SELECT status,count(*) AS count,
                    count(*) FILTER (WHERE status='SENDING' AND lease_until<=now()) AS expired_leases
                    FROM tripmate.reminder WHERE tenant_id=:tenant
                    AND status IN ('SENDING','RETRYING','FAILED') GROUP BY status ORDER BY status"""),
                {"tenant": ctx.tenant_id},
            )
        try:
            redis_status = "UP" if await runtime.redis.ping() else "DOWN"
        except Exception:
            redis_status = "DOWN"
        return envelope(
            request,
            {
                "redis": redis_status,
                "outbox": [dict(row) for row in outbox_rows.mappings()],
                "inbox": [dict(row) for row in inbox_rows.mappings()],
                "open_dead_letters_by_error": [dict(row) for row in dead_letters.mappings()],
                "failed_runs_by_error": [dict(row) for row in failed_runs.mappings()],
                "reminders": [dict(row) for row in reminder_rows.mappings()],
            },
        )

    @app.get("/v1/operations/metrics", tags=["operations"])
    async def operations_metrics(
        request: Request,
        window_hours: int = Query(default=24, ge=1, le=720),
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        runtime = service(request)
        async with runtime.sessions() as session:
            runs = await session.execute(
                text("""SELECT intent,status,count(*) AS count,
                    percentile_cont(0.95) WITHIN GROUP (ORDER BY
                      EXTRACT(epoch FROM updated_at-created_at)*1000)
                      FILTER (WHERE status IN ('COMPLETED','FAILED')) AS p95_duration_ms
                    FROM tripmate.agent_run WHERE tenant_id=:tenant
                      AND created_at>=now()-make_interval(hours=>:hours)
                    GROUP BY intent,status ORDER BY intent,status"""),
                {"tenant": ctx.tenant_id, "hours": window_hours},
            )
            reservations = await session.execute(
                text("""SELECT status,count(*) AS count FROM tripmate.reservation
                    WHERE tenant_id=:tenant AND created_at>=now()-make_interval(hours=>:hours)
                    GROUP BY status ORDER BY status"""),
                {"tenant": ctx.tenant_id, "hours": window_hours},
            )
            inventory = await session.execute(
                text("""SELECT count(*) AS slots,
                    COALESCE(sum(available_quantity),0) AS available,
                    COALESCE(sum(held_quantity),0) AS held,
                    COALESCE(sum(confirmed_quantity),0) AS confirmed,
                    count(*) FILTER (WHERE available_quantity+held_quantity+confirmed_quantity<>total_quantity)
                      AS invariant_violations
                    FROM tripmate.inventory WHERE tenant_id=:tenant"""),
                {"tenant": ctx.tenant_id},
            )
            events = await session.execute(
                text("""SELECT event_type,status,count(*) AS count FROM tripmate.outbox_event
                    WHERE tenant_id=:tenant AND created_at>=now()-make_interval(hours=>:hours)
                    GROUP BY event_type,status ORDER BY event_type,status"""),
                {"tenant": ctx.tenant_id, "hours": window_hours},
            )
            reminders = await session.execute(
                text("""SELECT reminder_type,status,count(*) AS count FROM tripmate.reminder
                    WHERE tenant_id=:tenant AND created_at>=now()-make_interval(hours=>:hours)
                    GROUP BY reminder_type,status ORDER BY reminder_type,status"""),
                {"tenant": ctx.tenant_id, "hours": window_hours},
            )
            inventory_summary = inventory.mappings().one()
        return envelope(
            request,
            {
                "window_hours": window_hours,
                "runs": [dict(row) for row in runs.mappings()],
                "reservations": [dict(row) for row in reservations.mappings()],
                "inventory": dict(inventory_summary),
                "outbox_events": [dict(row) for row in events.mappings()],
                "reminders": [dict(row) for row in reminders.mappings()],
            },
        )

    @app.post("/v1/trips", status_code=201, tags=["trips"])
    async def create_trip(
        body: CreateTripBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        runtime = service(request)
        data = await runtime.trips.create(
            ctx,
            destination=body.destination,
            start_date=body.start_date,
            end_date=body.end_date,
            timezone=body.timezone,
            party_size=body.party_size,
            budget_limit=body.budget_limit,
            currency=body.currency,
            companion_type=body.companion_type,
            idempotency_key=idempotency_key,
        )
        return envelope(request, data)

    @app.get("/v1/trips/{trip_id}", tags=["trips"])
    async def get_trip(
        trip_id: UUID,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        return envelope(request, await service(request).trips.get(ctx, trip_id))

    @app.post("/v1/trips/{trip_id}/confirm", tags=["trips"])
    async def confirm_trip(
        trip_id: UUID,
        body: TripConfirmBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).trips.confirm(
            ctx, trip_id, expected_version=body.expected_version, idempotency_key=idempotency_key
        )
        return envelope(request, result)

    @app.get("/v1/trips/{trip_id}/alternatives", tags=["trips"])
    async def list_trip_alternatives(
        trip_id: UUID,
        request: Request,
        slot_id: UUID = Query(),
        limit: int = Query(default=5, ge=1, le=20),
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        alternatives = await service(request).trips.alternatives(
            ctx, trip_id, slot_id, limit=limit
        )
        return envelope(request, {"alternatives": alternatives})

    @app.get("/v1/conversations/{conversation_id}/memory", tags=["conversations"])
    async def get_conversation_memory(
        conversation_id: UUID,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        memory = await service(request).short_term_memory.get(ctx, conversation_id)
        return envelope(request, {"memory": memory})

    @app.post("/v1/conversations/{conversation_id}/messages", status_code=202, tags=["runs"])
    async def create_run(
        conversation_id: UUID,
        body: CreateRunBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        receipt = await service(request).runs.create_run(
            ctx,
            conversation_id=conversation_id,
            message_id=uuid4(),
            message=body.message,
            trip_id=body.trip_id,
            idempotency_key=idempotency_key,
        )
        return envelope(request, receipt)

    @app.get("/v1/runs/{run_id}", tags=["runs"])
    async def get_run(
        run_id: UUID,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        run = await service(request).runs.get_run(ctx, run_id)
        result = run.pop("result_json", None)
        data = {**(result or {}), **run, "run_id": str(run_id)}
        return envelope(request, data)

    @app.get("/v1/runs/{run_id}/events", tags=["runs"])
    async def run_events(
        run_id: UUID,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        cursor = 0
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError as exc:
                raise DomainError("INVALID_ARGUMENT", "Last-Event-ID must be an integer", 400) from exc
            if cursor < 0:
                raise DomainError("INVALID_ARGUMENT", "Last-Event-ID must be non-negative", 400)
        runtime = service(request)
        await runtime.runs.get_run(ctx, run_id)

        async def stream() -> Any:
            nonlocal cursor
            last_heartbeat = asyncio.get_running_loop().time()
            while True:
                batch = await runtime.runs.get_run_event_batch(
                    ctx, run_id, after_sequence=cursor
                )
                events = batch["events"]
                for item in events:
                    cursor = int(item["event_sequence"])
                    data = json.dumps(
                        {
                            "event_id": str(item["event_id"]),
                            "run_id": str(run_id),
                            "occurred_at": item["occurred_at"].isoformat(),
                            "sequence": cursor,
                            "payload": item["payload"],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    event_name = "done" if item["event_type"] == "completed" else item["event_type"]
                    yield f"id: {cursor}\nevent: {event_name}\ndata: {data}\n\n"
                if not events and batch["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                    break
                now = asyncio.get_running_loop().time()
                if not events and now - last_heartbeat >= 15:
                    yield ": keep-alive\n\n"
                    last_heartbeat = now
                if not events:
                    await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/actions/{action_id}/confirmation", tags=["actions"])
    async def issue_action_confirmation(
        action_id: UUID,
        body: ActionConfirmationBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).reservations.issue_confirmation(
            ctx,
            action_id,
            expected_version=body.expected_confirmation_version,
            idempotency_key=idempotency_key,
        )
        return envelope(request, result)

    @app.post("/v1/actions/{action_id}/reject", tags=["actions"])
    async def reject_action(
        action_id: UUID,
        body: ActionRejectBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).reservations.reject_action(
            ctx,
            action_id,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return envelope(request, result)

    @app.post("/v1/actions/{action_id}/preference-confirmation", tags=["actions"])
    async def confirm_preference_action(
        action_id: UUID,
        body: ConfirmPreferenceActionBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).reservations.confirm_preference_action(
            ctx,
            action_id,
            confirmation_token=body.confirmation_token,
            idempotency_key=idempotency_key,
        )
        return envelope(request, result)

    @app.post("/v1/actions/{action_id}/waitlist", status_code=201, tags=["actions"])
    async def execute_waitlist_action(
        action_id: UUID,
        body: ConfirmPreferenceActionBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).reservations.execute_p1_action(
            ctx, action_id, action_type="propose_waitlist",
            confirmation_token=body.confirmation_token, idempotency_key=idempotency_key,
        )
        return envelope(request, result)

    @app.get("/v1/waitlist/promotions", tags=["reservations"])
    async def list_waitlist_promotions(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        promotions = await service(request).reservations.list_waitlist_promotions(ctx, limit=limit)
        return envelope(request, {"simulation": True, "promotions": promotions})

    @app.post("/v1/waitlist/promotions/{promotion_id}/confirm", tags=["reservations"])
    async def confirm_waitlist_promotion(
        promotion_id: UUID,
        body: ConfirmPreferenceActionBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).reservations.confirm_waitlist_promotion(
            ctx,
            promotion_id,
            confirmation_token=body.confirmation_token,
            idempotency_key=idempotency_key,
        )
        return envelope(request, {"simulation": True, **result})

    @app.post("/v1/actions/{action_id}/reminder-confirmation", status_code=201, tags=["actions"])
    async def execute_reminder_action(
        action_id: UUID,
        body: ConfirmPreferenceActionBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).reservations.execute_p1_action(
            ctx, action_id, action_type="create_reminder",
            confirmation_token=body.confirmation_token, idempotency_key=idempotency_key,
        )
        return envelope(request, result)

    @app.post("/v1/reservations", status_code=201, tags=["reservations"])
    async def hold_reservation(
        body: HoldReservationBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        runtime = service(request)
        async with runtime.sessions() as session:
            action = await session.execute(
                text("""SELECT a.parameters FROM tripmate.agent_action a
                    JOIN tripmate.agent_run r ON r.tenant_id=a.tenant_id AND r.run_id=a.run_id
                    WHERE a.tenant_id=:tenant AND a.action_id=:action AND r.user_id=:user
                      AND a.action_type='propose_reservation'"""),
                {"tenant": ctx.tenant_id, "action": body.action_id, "user": ctx.user_id},
            )
            parameters = action.scalar_one_or_none()
        if parameters is None:
            raise DomainError("NOT_FOUND", "Reservation action was not found", 404)
        if isinstance(parameters, str):
            parameters = json.loads(parameters)
        result = await runtime.reservations.hold(
            ctx,
            trip_id=UUID(str(parameters["trip_id"])),
            slot_id=body.slot_id,
            quantity=body.quantity,
            action_id=body.action_id,
            action_confirmation_token=body.confirmation_token,
            expected_inventory_version=body.expected_inventory_version,
            expected_activity_version=body.expected_activity_version,
            idempotency_key=idempotency_key,
        )
        payload = result.stable_payload()
        payload.update(
            {
                "hold_token": result.hold_token,
                "confirmation_token": result.confirmation_token,
                "replayed": result.replayed,
                "projection_pending": result.projection_pending,
            }
        )
        return envelope(request, payload)

    @app.post("/v1/reservations/{reservation_id}/confirm", tags=["reservations"])
    async def confirm_reservation(
        reservation_id: UUID,
        body: ConfirmReservationBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).reservations.confirm(
            ctx,
            reservation_id=reservation_id,
            action_id=body.action_id,
            confirmation_token=body.confirmation_token,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return envelope(request, result.stable_payload())

    @app.post("/v1/reservations/{reservation_id}/cancel", tags=["reservations"])
    async def cancel_reservation(
        reservation_id: UUID,
        body: CancelReservationBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result, refund = await service(request).reservations.cancel(
            ctx,
            reservation_id=reservation_id,
            action_id=body.action_id,
            action_confirmation_token=body.confirmation_token,
            expected_version=body.expected_version,
            reason=body.reason,
            idempotency_key=idempotency_key,
        )
        return envelope(request, {**result.stable_payload(), "refund_amount": str(refund)})

    @app.get("/v1/reservations/{reservation_id}", tags=["reservations"])
    async def get_reservation(
        reservation_id: UUID,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        runtime = service(request)
        async with runtime.sessions() as session:
            result = await session.execute(
                text("""SELECT reservation_id,trip_id,user_id,slot_id,quantity,price_snapshot,
                    currency,status,hold_expires_at,activity_version,inventory_version,version,
                    policy_version,created_at,updated_at FROM tripmate.reservation
                    WHERE tenant_id=:tenant AND reservation_id=:reservation AND user_id=:user"""),
                {"tenant": ctx.tenant_id, "reservation": reservation_id, "user": ctx.user_id},
            )
            row = result.mappings().one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Reservation was not found", 404)
        return envelope(request, dict(row))

    @app.get("/v1/preferences", tags=["preferences"])
    async def list_preferences(
        request: Request, ctx: RequestContext = Depends(context_dependency)
    ) -> dict[str, Any]:
        return envelope(request, await service(request).preferences.list_for_user(ctx))

    @app.get("/v1/preferences/cases/search", tags=["preferences"])
    async def search_preference_cases(
        request: Request,
        query: str = Query(min_length=1, max_length=500),
        limit: int = Query(default=5, ge=1, le=20),
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        preferences = await service(request).preferences.list_for_user(ctx)
        generation = max((int(item["generation"]) for item in preferences), default=1)
        cases = await service(request).knowledge.search_private_cases(
            ctx, query, current_generation=generation, limit=limit
        )
        return envelope(request, {"cases": cases})

    @app.get("/v1/notifications", tags=["notifications"])
    async def list_notifications(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        return envelope(request, await service(request).reminders.list_notifications(ctx, limit=limit))

    @app.post("/v1/notifications/{notification_id}/read", tags=["notifications"])
    async def mark_notification_read(
        notification_id: UUID,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        return envelope(request, await service(request).reminders.mark_read(ctx, notification_id))

    @app.post("/v1/preferences", tags=["preferences"])
    async def save_preference(
        body: PreferenceBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).preferences.save(
            ctx,
            scope=body.scope,
            key=body.key,
            value=body.value,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return envelope(request, result)

    @app.delete("/v1/preferences/{preference_id}", tags=["preferences"])
    async def delete_preference(
        preference_id: UUID,
        body: PreferenceDeleteBody,
        request: Request,
        ctx: RequestContext = Depends(context_dependency),
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ) -> dict[str, Any]:
        result = await service(request).preferences.delete(
            ctx,
            preference_id,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
        )
        return envelope(request, result)

    @app.get("/v1/knowledge/search", tags=["knowledge"])
    async def search_knowledge(
        request: Request,
        query: str = Query(min_length=1, max_length=1000),
        top_k: int = Query(default=5, ge=1, le=20),
        ctx: RequestContext = Depends(context_dependency),
    ) -> dict[str, Any]:
        results, diagnostics = await service(request).knowledge.search_with_diagnostics(
            ctx, query, top_k=top_k
        )
        return envelope(
            request,
            {
                "citations": [item.model_dump(mode="json") for item in results],
                "diagnostics": diagnostics,
            },
        )

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["TripMateBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


def _error_response(request: Request, error: DomainError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid4()))
    trace_id = getattr(request.state, "trace_id", request_id.replace("-", ""))
    content = {
        "request_id": request_id,
        "trace_id": trace_id,
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "details": jsonable_encoder(error.details),
        },
    }
    return JSONResponse(status_code=error.http_status, content=content)


app = create_app()
