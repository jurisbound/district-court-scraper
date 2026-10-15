"""Database connection and session management.

v2 targets Supabase Postgres via Supavisor (pgbouncer-like) connection poolers:
  - Transaction Pooler (port 6543) for the app — short-lived connections.
  - Session Pooler  (port 5432) for Alembic / long-lived schema ops.

The Transaction Pooler runs pgbouncer in transaction mode, which does NOT
support prepared statements. asyncpg uses prepared statements by default, so
we disable its statement cache when talking to that pooler.

Detection is by URL: if the host contains 'pooler.supabase.com' and port is
6543, we treat it as the transaction pooler. Local Postgres URLs are left
alone (statement cache stays enabled — local doesn't have this restriction).
"""
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from configs.settings import Settings

settings = Settings()


def _is_supabase_transaction_pooler(url: str) -> bool:
    return "pooler.supabase.com" in url and ":6543" in url


# ── Async engine (FastAPI / scrapers) ────────────────────────────────────────
_async_kwargs = {
    "echo": False,
    "pool_pre_ping": True,
    "pool_size": 10,
    "max_overflow": 20,
}
if _is_supabase_transaction_pooler(settings.DATABASE_URL):
    # asyncpg-specific: disable prepared statement cache + use unique names so
    # pgbouncer transaction mode doesn't see name collisions across pooled
    # connections. Both are required, not optional.
    _async_kwargs["connect_args"] = {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{__import__('uuid').uuid4().hex}__",
    }

async_engine = create_async_engine(settings.DATABASE_URL, **_async_kwargs)
AsyncSessionLocal = async_sessionmaker(async_engine, expire_on_commit=False)


# ── Sync engine (Alembic) ────────────────────────────────────────────────────
sync_engine = create_engine(settings.SYNC_DATABASE_URL, pool_pre_ping=True)
SyncSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sync_engine)


@asynccontextmanager
async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    """Create all tables (use Alembic for production; this is a dev convenience)."""
    from storage.models import Base
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
