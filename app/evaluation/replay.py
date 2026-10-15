import argparse
import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.runs import AgentRunService
from app.config import get_settings
from app.domain.context import RequestContext
from app.domain.errors import DomainError
from app.domain.idempotency import canonical_json, sha256_hex
from app.domain.ports import Clock, IdGenerator
from app.persistence.postgres.seed import seed_id
from app.runtime import Runtime


class EvaluationExpectations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str | None = Field(default=None, min_length=1)
    expected_tools: list[str] | None = Field(default=None, max_length=100)
    allowed_tools: list[str] | None = Field(default=None, max_length=100)
    constraint_state: str | None = Field(default=None, min_length=1)
    allow_unknown: bool = False
    required_citation_ids: list[str] | None = Field(default=None, max_length=100)
    reservation_status: str | None = None

    @model_validator(mode="after")
    def require_assertion(self) -> "EvaluationExpectations":
        asserted_fields = self.model_fields_set - {"allow_unknown"}
        if not asserted_fields:
            raise ValueError("at least one evaluation expectation is required")
        nullable_fields = asserted_fields - {"reservation_status"}
        if any(getattr(self, field) is None for field in nullable_fields):
            raise ValueError("evaluation expectations must not be null")
        return self


def compare_case(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, bool]:
    try:
        expectations = EvaluationExpectations.model_validate(expected)
    except ValidationError:
        return {"expectation_schema": False}

    fields = expectations.model_fields_set
    checks: dict[str, bool] = {"expectation_schema": True}
    if "intent" in fields:
        checks["intent"] = actual.get("intent") == expectations.intent
    if "expected_tools" in fields:
        checks["tool_sequence"] = actual.get("tools", []) == expectations.expected_tools
    if "allowed_tools" in fields:
        allowed = set(expectations.allowed_tools or [])
        checks["tool_allowlist"] = all(tool in allowed for tool in actual.get("tools", []))
    if "constraint_state" in fields:
        unknown_allowed = expectations.allow_unknown and actual.get("constraint_state") == "UNKNOWN"
        checks["constraint_state"] = unknown_allowed or (
            actual.get("constraint_state") == expectations.constraint_state
        )
    if "required_citation_ids" in fields:
        checks["citations"] = set(expectations.required_citation_ids or []) <= set(
            actual.get("citation_ids", [])
        )
    if "reservation_status" in fields:
        checks["reservation_status"] = actual.get("reservation_status") == expectations.reservation_status
    return checks


class EvaluationReplayer:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        runs: AgentRunService,
        clock: Clock,
        ids: IdGenerator,
        *,
        timeout_seconds: int = 300,
    ) -> None:
        self.sessions = sessions
        self.runs = runs
        self.clock = clock
        self.ids = ids
        self.timeout_seconds = timeout_seconds

    async def replay_case(self, tenant_id: UUID, case_id: UUID) -> dict[str, Any]:
        case = await self._load_case(tenant_id, case_id)
        user_id = case["user_id"]
        input_data = _as_mapping(case["input_json"])
        conversation_id = UUID(str(case["conversation_id"]))
        trip_id = UUID(str(case["trip_id"])) if case["trip_id"] else None
        now = self.clock.now()
        request_id = f"evaluation:{uuid4()}"
        ctx = RequestContext(
            request_id=request_id,
            trace_id=request_id.replace(":", "-"),
            tenant_id=tenant_id,
            user_id=user_id,
            roles=("evaluator",),
            deadline=now + timedelta(seconds=self.timeout_seconds),
            source="replay",
            trip_id=trip_id,
        )
        receipt = await self.runs.create_run(
            ctx,
            conversation_id=conversation_id,
            message_id=self.ids.new(),
            message=str(input_data["message"]),
            trip_id=trip_id,
            idempotency_key=f"evaluation:{case_id}:{case['case_version']}:{self.ids.new()}",
        )
        deadline = self.clock.now() + timedelta(seconds=self.timeout_seconds)
        while self.clock.now() < deadline:
            run = await self.runs.get_run(ctx, receipt.run_id)
            if run["status"] == "COMPLETED":
                return await self.evaluate_run(
                    ctx, case_id, receipt.run_id, case_version=int(case["case_version"])
                )
            if run["status"] == "FAILED":
                return await self._save_result(
                    case,
                    receipt.run_id,
                    "RUN_FAILED",
                    {"run_status": "FAILED", "error_code": run.get("last_error_code")},
                    {},
                )
            if run["status"] == "WAITING_CONFIRMATION":
                return await self._save_result(
                    case, receipt.run_id, "WAITING_CONFIRMATION", {"run_status": run["status"]}, {}
                )
            await asyncio.sleep(0.25)
        return await self._save_result(
            case, receipt.run_id, "TIMED_OUT", {"run_status": "RUNNING"}, {}
        )

    async def evaluate_run(
        self,
        ctx: RequestContext,
        case_id: UUID,
        run_id: UUID,
        *,
        case_version: int | None = None,
    ) -> dict[str, Any]:
        case = await self._load_case(ctx.tenant_id, case_id, case_version=case_version)
        run = await self.runs.get_run(ctx, run_id)
        if run["status"] != "COMPLETED":
            raise DomainError("RUN_NOT_COMPLETE", "Evaluation requires a completed run", 409, True)
        stored = _as_mapping(run.get("result_json"))
        report = _as_mapping(stored.get("result"))
        constraints = _as_mapping(stored.get("constraint_result") or report.get("constraint_result"))
        citations_value = stored.get("citations") or report.get("citations") or []
        citations = citations_value if isinstance(citations_value, list) else []
        async with self.sessions() as session:
            tools_result = await session.execute(
                text("""SELECT tool_name FROM tripmate.agent_tool_call
                    WHERE tenant_id=:tenant AND run_id=:run
                    ORDER BY created_at,tool_call_id"""),
                {"tenant": ctx.tenant_id, "run": run_id},
            )
            reservation_result = await session.execute(
                text("""SELECT r.status FROM tripmate.reservation r
                    JOIN tripmate.agent_action a ON a.tenant_id=r.tenant_id
                      AND a.action_id=r.approved_action_id
                    WHERE a.tenant_id=:tenant AND a.run_id=:run
                    ORDER BY r.created_at,r.reservation_id"""),
                {"tenant": ctx.tenant_id, "run": run_id},
            )
        tools = [str(row[0]) for row in tools_result]
        reservations = [str(row[0]) for row in reservation_result]
        actual = {
            "intent": report.get("intent", stored.get("intent")),
            "tools": tools,
            "constraint_state": constraints.get("state"),
            "constraint_result": constraints,
            "citations": [
                {
                    key: citation[key]
                    for key in (
                        "chunk_id",
                        "document_id",
                        "source_version",
                        "source_url",
                        "index_version",
                        "content_sha256",
                    )
                    if key in citation
                }
                for citation in citations
                if isinstance(citation, dict)
            ],
            "citation_ids": [
                str(citation["chunk_id"])
                for citation in citations
                if isinstance(citation, dict) and citation.get("chunk_id")
            ],
            "reservation_status": reservations[-1] if reservations else None,
            "reservation_statuses": reservations,
        }
        expected = _as_mapping(case["expected_json"])
        assertions = compare_case(expected, actual)
        status = "PASSED" if all(assertions.values()) else "FAILED"
        return await self._save_result(case, run_id, status, actual, assertions)

    async def _load_case(
        self, tenant_id: UUID, case_id: UUID, *, case_version: int | None = None
    ) -> Any:
        async with self.sessions() as session:
            result = await session.execute(
                text("""SELECT tenant_id,case_id,case_version,user_id,conversation_id,trip_id,
                    input_json,expected_json FROM tripmate.evaluation_case
                    WHERE tenant_id=:tenant AND case_id=:case AND active
                      AND (:case_version IS NULL OR case_version=:case_version)
                    ORDER BY case_version DESC LIMIT 1"""),
                {"tenant": tenant_id, "case": case_id, "case_version": case_version},
            )
            case = result.mappings().one_or_none()
        if case is None:
            raise DomainError("NOT_FOUND", "Evaluation case was not found", 404)
        return case

    async def _save_result(
        self,
        case: Any,
        run_id: UUID,
        status: str,
        actual: dict[str, Any],
        assertions: dict[str, bool],
    ) -> dict[str, Any]:
        body = {
            "case_id": str(case["case_id"]),
            "case_version": int(case["case_version"]),
            "run_id": str(run_id),
            "status": status,
            "actual": actual,
            "assertions": assertions,
        }
        result_hash = sha256_hex(canonical_json(body))
        async with self.sessions() as session, session.begin():
            await session.execute(
                text("""INSERT INTO tripmate.evaluation_result
                    (tenant_id,case_id,case_version,run_id,status,actual_json,assertions_json,result_hash)
                    VALUES(:tenant,:case,:case_version,:run,:status,CAST(:actual AS jsonb),
                      CAST(:assertions AS jsonb),:hash)
                    ON CONFLICT (tenant_id,case_id,case_version,run_id) DO UPDATE SET
                      status=EXCLUDED.status,actual_json=EXCLUDED.actual_json,
                      assertions_json=EXCLUDED.assertions_json,result_hash=EXCLUDED.result_hash"""),
                {
                    "tenant": case["tenant_id"],
                    "case": case["case_id"],
                    "case_version": case["case_version"],
                    "run": run_id,
                    "status": status,
                    "actual": json.dumps(actual),
                    "assertions": json.dumps(assertions),
                    "hash": result_hash,
                },
            )
        return {**body, "result_hash": result_hash}


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


async def main() -> None:
    parser = argparse.ArgumentParser(description="Replay one fixed TripMate evaluation case")
    parser.add_argument("--tenant-id", type=UUID, default=seed_id("tenant:demo"))
    parser.add_argument("--case-id", type=UUID, default=seed_id("evaluation:demo-planning"))
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    runtime = await Runtime.create(get_settings())
    try:
        result = await EvaluationReplayer(
            runtime.sessions,
            runtime.runs,
            runtime.reservations.clock,
            runtime.reservations.ids,
            timeout_seconds=args.timeout,
        ).replay_case(args.tenant_id, args.case_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] != "PASSED":
            raise SystemExit(1)
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
