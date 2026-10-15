from dataclasses import dataclass

from langgraph.checkpoint.redis import AsyncRedisSaver
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agent.runs import AgentRunService
from app.config import Settings, get_settings
from app.domain.ports import RandomIdGenerator, SystemClock
from app.inventory.redis_projection import RedisInventoryProjection
from app.knowledge.retriever import KnowledgeRetriever
from app.memory.preferences import PreferenceService
from app.memory.short_term import ShortTermMemoryService
from app.observability.otel import initialize_telemetry
from app.persistence.postgres.database import create_engine, session_factory
from app.reservation.service import ReservationService
from app.tools.gateway import ToolGateway
from app.tools.realtime import RealtimeAdapters
from app.trips.service import TripService
from redis.asyncio import Redis


@dataclass(slots=True)
class Runtime:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    redis: Redis
    checkpointer: AsyncRedisSaver
    projection: RedisInventoryProjection
    realtime: RealtimeAdapters
    knowledge: KnowledgeRetriever
    preferences: PreferenceService
    short_term_memory: ShortTermMemoryService
    reservations: ReservationService
    runs: AgentRunService
    trips: TripService
    tools: ToolGateway

    @classmethod
    async def create(cls, settings: Settings | None = None) -> "Runtime":
        config = settings or get_settings()
        initialize_telemetry(config)
        engine = create_engine(config)
        sessions = session_factory(engine)
        redis = Redis.from_url(config.redis_url, decode_responses=True)
        checkpointer = AsyncRedisSaver(redis_client=redis)
        projection = RedisInventoryProjection(redis, config, sessions)
        realtime = RealtimeAdapters(sessions, config)
        knowledge = KnowledgeRetriever(sessions, config)
        preferences = PreferenceService(sessions, config, SystemClock(), RandomIdGenerator(), redis)
        short_term_memory = ShortTermMemoryService(redis, config, SystemClock(), sessions)
        reservations = ReservationService(
            sessions, config, SystemClock(), RandomIdGenerator(), projection
        )
        runs = AgentRunService(sessions, config, SystemClock(), RandomIdGenerator())
        trips = TripService(sessions, config, SystemClock(), RandomIdGenerator())
        tools = ToolGateway(sessions, config, realtime, knowledge, reservations, preferences, redis)
        try:
            await redis.ping()
            await projection.setup()
            await short_term_memory.setup()
            await checkpointer.setup()
        except Exception:
            await realtime.close()
            await redis.aclose()
            await engine.dispose()
            raise
        return cls(
            config,
            engine,
            sessions,
            redis,
            checkpointer,
            projection,
            realtime,
            knowledge,
            preferences,
            short_term_memory,
            reservations,
            runs,
            trips,
            tools,
        )

    async def close(self) -> None:
        await self.realtime.close()
        await self.redis.aclose()
        await self.engine.dispose()
