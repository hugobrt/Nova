"""
Comptes STAFF — authentification par identifiant/mot de passe, totalement
indépendante du système OAuth Discord et de la base de données principale.

Utilise SA PROPRE connexion Postgres (variable d'env STAFF_DATABASE_URL),
idéalement sur un compte Aiven différent de la base principale, pour que
ce système de connexion reste disponible même en cas de souci sur la BDD
principale.

Un compte staff ne donne accès qu'à un dashboard réduit : employés,
congés, incidents, tickets, tribunal/prison. Rien d'autre (pas de
config, pas d'offres d'emploi, pas d'annonces).
"""

import os
import logging
import secrets

import asyncpg
import bcrypt

logger = logging.getLogger("StaffAuth")

STAFF_DATABASE_URL = os.getenv("STAFF_DATABASE_URL")

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff_accounts (
    id SERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);
"""


async def init_staff_db() -> asyncpg.Pool | None:
    global _pool
    if _pool is not None:
        return _pool

    if not STAFF_DATABASE_URL:
        logger.warning("STAFF_DATABASE_URL n'est pas défini — le système de comptes staff est désactivé.")
        return None

    _pool = await asyncpg.create_pool(
        dsn=STAFF_DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
        ssl="require",
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)
    logger.info("Pool de connexions Postgres (Aiven — staff) initialisé.")
    return _pool


async def close_staff_db():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Pool de connexions Postgres (staff) fermé.")


def is_staff_enabled() -> bool:
    return _pool is not None


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def generate_password(length: int = 10) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def create_staff_account(guild_id: int, username: str, display_name: str, password: str, created_by: int) -> int:
    if _pool is None:
        raise RuntimeError("Base de données staff non initialisée.")
    password_hash = _hash_password(password)
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO staff_accounts (guild_id, username, password_hash, display_name, created_by)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            guild_id, username.lower().strip(), password_hash, display_name.strip(), created_by,
        )
    return row["id"]


async def list_staff_accounts(guild_id: int) -> list[dict]:
    if _pool is None:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, guild_id, username, display_name, active, created_by, created_at, last_login_at
               FROM staff_accounts WHERE guild_id = $1 ORDER BY created_at DESC""",
            guild_id,
        )
    return [dict(r) for r in rows]


async def get_staff_account_by_username(username: str) -> dict | None:
    if _pool is None:
        return None
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM staff_accounts WHERE username = $1", username.lower().strip()
        )
    return dict(row) if row else None


async def verify_staff_login(username: str, password: str) -> dict | None:
    account = await get_staff_account_by_username(username)
    if account is None or not account["active"]:
        return None
    if not _verify_password(password, account["password_hash"]):
        return None
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE staff_accounts SET last_login_at = now() WHERE id = $1", account["id"])
    return account


async def set_staff_active(account_id: int, active: bool):
    if _pool is None:
        return
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE staff_accounts SET active = $1 WHERE id = $2", active, account_id)


async def reset_staff_password(account_id: int, new_password: str):
    if _pool is None:
        return
    password_hash = _hash_password(new_password)
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE staff_accounts SET password_hash = $1 WHERE id = $2", password_hash, account_id)


async def delete_staff_account(account_id: int, guild_id: int):
    if _pool is None:
        return
    async with _pool.acquire() as conn:
        await conn.execute("DELETE FROM staff_accounts WHERE id = $1 AND guild_id = $2", account_id, guild_id)
