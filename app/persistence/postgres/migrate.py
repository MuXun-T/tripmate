import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import text

from app.persistence.postgres.database import create_engine

MIGRATIONS = Path(__file__).resolve().parents[3] / "migrations"


async def apply_migrations() -> list[str]:
    engine = create_engine()
    applied: list[str] = []
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    """CREATE TABLE IF NOT EXISTS public.tripmate_migration_history (
                        version text PRIMARY KEY,
                        checksum char(64) NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )"""
                )
            )
            await connection.commit()
            await connection.execute(text("SELECT pg_advisory_lock(741920261014)"))
            await connection.commit()
            try:
                for path in sorted(MIGRATIONS.glob("V*__*.sql")):
                    sql = path.read_text(encoding="utf-8")
                    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    async with connection.begin():
                        existing = await connection.scalar(
                            text(
                                "SELECT checksum FROM public.tripmate_migration_history "
                                "WHERE version=:version"
                            ),
                            {"version": path.name},
                        )
                        if existing is not None:
                            if existing != checksum:
                                raise RuntimeError(
                                    f"Applied migration checksum changed: {path.name}"
                                )
                            continue
                        await connection.exec_driver_sql(sql)
                        await connection.execute(
                            text(
                                "INSERT INTO public.tripmate_migration_history(version, checksum) "
                                "VALUES (:version, :checksum)"
                            ),
                            {"version": path.name, "checksum": checksum},
                        )
                    applied.append(path.name)
            finally:
                if connection.in_transaction():
                    await connection.rollback()
                await connection.execute(text("SELECT pg_advisory_unlock(741920261014)"))
                await connection.commit()
        return applied
    finally:
        await engine.dispose()


async def main() -> None:
    versions = await apply_migrations()
    print("Applied migrations:" if versions else "Migrations already current")
    for version in versions:
        print(version)


if __name__ == "__main__":
    asyncio.run(main())
