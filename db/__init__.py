"""Database connection pool using asyncpg."""
import logging
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger(__name__)

_pool = None


async def init_db(database_url: str) -> None:
    """Create asyncpg connection pool. Called once at startup."""
    global _pool
    try:
        import asyncpg
        _pool = await asyncpg.create_pool(
            database_url,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("Database pool created: %s", database_url.split("@")[-1] if "@" in database_url else "local")
    except ImportError:
        logger.warning("asyncpg not installed — database features disabled")
    except Exception as e:
        logger.error("Database connection failed: %s", e)
        _pool = None


async def close_db() -> None:
    """Close connection pool."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


def get_pool():
    """Get the connection pool (may be None if DB not configured)."""
    return _pool


@asynccontextmanager
async def get_conn():
    """Get a database connection from the pool."""
    if _pool is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    async with _pool.acquire() as conn:
        yield conn


async def run_migrations() -> None:
    """Run SQL migration files in order."""
    import os
    migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
    if not os.path.exists(migrations_dir):
        return

    if _pool is None:
        logger.warning("Cannot run migrations — no database pool")
        return

    async with _pool.acquire() as conn:
        # Create migrations tracking table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                filename VARCHAR(255) PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Get already applied migrations
        applied = set()
        rows = await conn.fetch("SELECT filename FROM _migrations")
        for row in rows:
            applied.add(row["filename"])

        # Run new migrations
        sql_files = sorted(f for f in os.listdir(migrations_dir) if f.endswith(".sql"))
        for sql_file in sql_files:
            if sql_file in applied:
                continue
            filepath = os.path.join(migrations_dir, sql_file)
            sql = open(filepath).read()
            try:
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO _migrations (filename) VALUES ($1)", sql_file
                )
                logger.info("Migration applied: %s", sql_file)
            except Exception as e:
                logger.error("Migration failed (%s): %s", sql_file, e)
                raise
