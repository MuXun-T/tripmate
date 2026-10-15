import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.persistence.postgres.database import create_engine

SEED_NAMESPACE = UUID("e99ca578-5cf4-4fc3-b188-fae45e335311")


def seed_id(value: str) -> UUID:
    return uuid5(SEED_NAMESPACE, value)


async def seed_demo_data() -> None:
    engine = create_engine()
    tenant_id = seed_id("tenant:demo")
    user_id = seed_id("user:demo")
    trip_id = seed_id("trip:demo-evaluation")
    conversation_id = seed_id("conversation:demo-evaluation")
    evaluation_case_id = seed_id("evaluation:demo-planning")
    attraction_id = seed_id("attraction:west-lake")
    manifest_id = seed_id("manifest:demo-v1")
    document_id = seed_id("document:west-lake-policy")
    chunk_id = seed_id("chunk:west-lake-policy:0")
    slot_id = seed_id("slot:west-lake:morning")
    local_zone = ZoneInfo("Asia/Shanghai")
    visit_day = date.today() + timedelta(days=4)
    start_at = datetime.combine(visit_day, time(9), local_zone).astimezone(UTC)
    end_at = datetime.combine(visit_day, time(11), local_zone).astimezone(UTC)
    policy = {
        "policy_id": "demo-standard",
        "policy_version": "2026-10-v1",
        "timezone": "Asia/Shanghai",
        "source": "operator",
        "rules": [
            {
                "cutoff_at": (start_at - timedelta(days=1)).isoformat(),
                "refund_percent": "100.00",
                "reason_code": "CANCEL_BEFORE_ONE_DAY",
            }
        ],
    }
    content = (
        "西湖演示景点预约说明：请提前预约入园时段。该知识仅用于解释演示规则；"
        "实时开放状态、票价和库存必须通过实时工具查询。"
    )
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO tripmate.tenant(tenant_id,name,status) VALUES(:id,'demo','ACTIVE') "
                    "ON CONFLICT (tenant_id) DO NOTHING"
                ),
                {"id": tenant_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO tripmate.user_profile(user_id,tenant_id,display_name,status) "
                    "VALUES(:user,:tenant,'Demo User','ACTIVE') ON CONFLICT (user_id) DO NOTHING"
                ),
                {"user": user_id, "tenant": tenant_id},
            )
            await conn.execute(
                text("""INSERT INTO tripmate.attraction
                    (attraction_id,tenant_id,external_source,external_id,city,name,category,
                     description,source_version,status)
                    VALUES(:id,:tenant,'tripmate-demo','west-lake','hangzhou','西湖演示景点',
                     'scenic','本地演示数据，不代表实时运营信息','demo-v1','ACTIVE')
                    ON CONFLICT (tenant_id,external_source,external_id) DO NOTHING"""),
                {"id": attraction_id, "tenant": tenant_id},
            )
            await conn.execute(
                text("""INSERT INTO tripmate.activity_slot
                    (slot_id,tenant_id,attraction_id,external_id,start_at,end_at,capacity,price,
                     currency,cancellation_policy,policy_version,activity_version,status)
                    VALUES(:id,:tenant,:attraction,'morning',:start,:end,40,35.00,'CNY',
                     CAST(:policy AS jsonb),'2026-10-v1',1,'ACTIVE')
                    ON CONFLICT (tenant_id,slot_id) DO UPDATE SET
                      start_at=EXCLUDED.start_at,end_at=EXCLUDED.end_at,
                      cancellation_policy=EXCLUDED.cancellation_policy,
                      policy_version=EXCLUDED.policy_version"""),
                {
                    "id": slot_id,
                    "tenant": tenant_id,
                    "attraction": attraction_id,
                    "start": start_at,
                    "end": end_at,
                    "policy": json.dumps(policy),
                },
            )
            await conn.execute(
                text("""INSERT INTO tripmate.inventory
                    (tenant_id,slot_id,total_quantity,available_quantity,status)
                    VALUES(:tenant,:slot,40,40,'ACTIVE') ON CONFLICT (tenant_id,slot_id) DO NOTHING"""),
                {"tenant": tenant_id, "slot": slot_id},
            )
            await conn.execute(
                text("""INSERT INTO tripmate.trip
                    (trip_id,tenant_id,owner_user_id,destination,start_date,end_date,timezone,
                     party_size,budget_limit,currency,companion_type,status)
                    VALUES(:trip,:tenant,:user,'hangzhou',:start,:end,'Asia/Shanghai',2,300.00,
                     'CNY','couple','PLANNING')
                    ON CONFLICT (tenant_id,trip_id) DO UPDATE SET
                      start_date=EXCLUDED.start_date,end_date=EXCLUDED.end_date,
                      party_size=EXCLUDED.party_size,budget_limit=EXCLUDED.budget_limit,
                      status='PLANNING'"""),
                {
                    "trip": trip_id,
                    "tenant": tenant_id,
                    "user": user_id,
                    "start": visit_day,
                    "end": visit_day,
                },
            )
            await conn.execute(
                text("""INSERT INTO tripmate.knowledge_manifest
                    (manifest_id,tenant_id,index_version,index_alias,embedding_model,vector_dimensions,
                     status,document_count,chunk_count,validated_at)
                    VALUES(:id,:tenant,'demo-v1','tm:knowledge:v1:dev:active','lexical-v1',1,
                     'ACTIVE',1,1,now()) ON CONFLICT (tenant_id,index_version) DO NOTHING"""),
                {"id": manifest_id, "tenant": tenant_id},
            )
            await conn.execute(
                text("""INSERT INTO tripmate.knowledge_document
                    (document_id,tenant_id,external_document_id,document_type,source,source_id,
                     source_url,source_version,title,city,content_sha256,retrieved_at,status)
                    VALUES(:id,:tenant,'west-lake-policy','booking_policy','operator','west-lake',
                     'https://example.invalid/tripmate/demo-policy','2026-10-v1','演示预约说明',
                     'hangzhou',:hash,now(),'ACTIVE')
                    ON CONFLICT (tenant_id,external_document_id,source_version) DO NOTHING"""),
                {"id": document_id, "tenant": tenant_id, "hash": content_hash},
            )
            await conn.execute(
                text("""INSERT INTO tripmate.knowledge_chunk
                    (chunk_id,tenant_id,document_id,manifest_id,ordinal,heading,content,content_sha256)
                    VALUES(:id,:tenant,:document,:manifest,0,'预约与实时数据边界',:content,:hash)
                    ON CONFLICT (tenant_id,manifest_id,document_id,ordinal) DO NOTHING"""),
                {
                    "id": chunk_id,
                    "tenant": tenant_id,
                    "document": document_id,
                    "manifest": manifest_id,
                    "content": content,
                    "hash": content_hash,
                },
            )
            await conn.execute(
                text("""INSERT INTO tripmate.knowledge_active_alias
                    (tenant_id,index_alias,manifest_id,generation)
                    VALUES(:tenant,'tm:knowledge:v1:dev:active',:manifest,1)
                    ON CONFLICT (tenant_id,index_alias) DO NOTHING"""),
                {"tenant": tenant_id, "manifest": manifest_id},
            )
            await conn.execute(
                text("""INSERT INTO tripmate.evaluation_case
                    (tenant_id,case_id,case_version,user_id,conversation_id,trip_id,name,input_json,
                     expected_json,active)
                    VALUES(:tenant,:case,1,:user,:conversation,:trip,'demo-planning',
                     CAST(:input AS jsonb),CAST(:expected AS jsonb),true)
                    ON CONFLICT (tenant_id,case_id,case_version) DO UPDATE SET
                      user_id=EXCLUDED.user_id,conversation_id=EXCLUDED.conversation_id,
                      trip_id=EXCLUDED.trip_id,name=EXCLUDED.name,input_json=EXCLUDED.input_json,
                      expected_json=EXCLUDED.expected_json,active=true"""),
                {
                    "tenant": tenant_id,
                    "case": evaluation_case_id,
                    "user": user_id,
                    "conversation": conversation_id,
                    "trip": trip_id,
                    "input": json.dumps(
                        {
                            "message": "请为我的杭州一日游规划行程并核对景点时段。",
                        },
                        ensure_ascii=False,
                    ),
                    "expected": json.dumps(
                        {
                            "intent": "planning",
                            "expected_tools": ["get_trip_context"],
                            "allowed_tools": ["get_trip_context"],
                            "constraint_state": "PASS",
                            "required_citation_ids": [],
                            "reservation_status": None,
                        }
                    ),
                },
            )
    finally:
        await engine.dispose()


async def main() -> None:
    await seed_demo_data()
    print(f"Demo tenant={seed_id('tenant:demo')} user={seed_id('user:demo')}")


if __name__ == "__main__":
    asyncio.run(main())
