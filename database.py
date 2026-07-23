"""
Connexion et accès à la base de données (Postgres, hébergée sur Aiven).
Pool de connexions partagé par le bot ET l'API (même processus).
"""

import os
import logging

import asyncpg

logger = logging.getLogger("Database")

DATABASE_URL = os.getenv("DATABASE_URL")

_pool: asyncpg.Pool | None = None


async def init_db() -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL n'est pas défini dans l'environnement (URL de connexion Aiven).")

    _pool = await asyncpg.create_pool(
        dsn=DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
        ssl="require",
    )
    logger.info("Pool de connexions Postgres (Aiven) initialisé.")
    await _run_migrations()
    return _pool


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Le pool n'est pas encore initialisé, appelle init_db() d'abord.")
    return _pool


async def close_db():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Pool de connexions Postgres fermé.")


async def fetch_one(query: str, *params) -> dict | None:
    pool = get_pool()
    row = await pool.fetchrow(query, *params)
    return dict(row) if row else None


async def fetch_all(query: str, *params) -> list[dict]:
    pool = get_pool()
    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


async def execute(query: str, *params):
    pool = get_pool()
    return await pool.execute(query, *params)


async def fetch_val(query: str, *params):
    pool = get_pool()
    return await pool.fetchval(query, *params)


SCHEMA = """
CREATE TABLE IF NOT EXISTS server_config (
    guild_id BIGINT PRIMARY KEY,
    rules_channel_id BIGINT,
    welcome_channel_id BIGINT,
    member_role_id BIGINT,
    jobs_channel_id BIGINT,
    application_log_channel_id BIGINT,
    employee_role_id BIGINT,
    hr_role_id BIGINT,
    employee_portal_channel_id BIGINT,
    hr_log_channel_id BIGINT,
    tickets_category_id BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_offers (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    role_id BIGINT,
    posted_by BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    message_id BIGINT,
    channel_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_applications (
    id SERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES job_offers(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL,
    motivation TEXT,
    disponibilite TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS employees (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    role_id BIGINT,
    status TEXT NOT NULL DEFAULT 'active',
    hired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sanction_reason TEXT,
    sanctioned_at TIMESTAMPTZ,
    sanctioned_by BIGINT,
    fired_reason TEXT,
    fired_at TIMESTAMPTZ,
    fired_by BIGINT,
    UNIQUE (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS leave_requests (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_by BIGINT,
    reviewed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS incident_reports (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    incident_type TEXT NOT NULL,
    line_info TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by BIGINT,
    resolved_at TIMESTAMPTZ,
    resolution_note TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    channel_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_by BIGINT,
    closed_at TIMESTAMPTZ
);
"""

MIGRATIONS_ALTER = [
    "ALTER TABLE server_config ADD COLUMN IF NOT EXISTS employee_portal_channel_id BIGINT",
    "ALTER TABLE server_config ADD COLUMN IF NOT EXISTS hr_log_channel_id BIGINT",
    "ALTER TABLE server_config ADD COLUMN IF NOT EXISTS tickets_category_id BIGINT",
]


async def _run_migrations():
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA)
        for stmt in MIGRATIONS_ALTER:
            try:
                await conn.execute(stmt)
            except Exception as e:
                logger.warning(f"Migration ignorée ({stmt}) : {e}")
    logger.info("Schéma de base de données vérifié/appliqué.")
