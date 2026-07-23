"""
Sanctions ("tribunal" RH) et peines de prison — Postgres (Aiven).
"""

import logging

from database import fetch_one, fetch_all, execute, fetch_val

logger = logging.getLogger("SanctionsStore")


# ---------------------------------------------------------------------
# Sanctions
# ---------------------------------------------------------------------

async def create_sanction(guild_id: int, user_id: int, reason: str, sanctioned_by: int) -> int:
    return await fetch_val(
        """INSERT INTO sanctions (guild_id, user_id, reason, sanctioned_by)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        guild_id, user_id, reason, sanctioned_by,
    )


async def attach_sanction_channel(sanction_id: int, channel_id: int, message_id: int | None = None):
    await execute(
        "UPDATE sanctions SET channel_id = $1, message_id = $2 WHERE id = $3",
        channel_id, message_id, sanction_id,
    )


async def get_sanction(sanction_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM sanctions WHERE id = $1", sanction_id)


async def get_sanction_by_channel(channel_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM sanctions WHERE channel_id = $1", channel_id)


async def list_sanctions(guild_id: int, status: str | None = None) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM sanctions WHERE guild_id = $1 AND status = $2 ORDER BY created_at DESC",
            guild_id, status,
        )
    return await fetch_all("SELECT * FROM sanctions WHERE guild_id = $1 ORDER BY created_at DESC", guild_id)


async def set_contest_message(sanction_id: int, message: str):
    await execute(
        "UPDATE sanctions SET contest_message = $1, status = 'contested' WHERE id = $2",
        message, sanction_id,
    )


async def cancel_sanction(sanction_id: int, resolved_by: int):
    await execute(
        "UPDATE sanctions SET status = 'cancelled', resolved_by = $1, resolved_at = now() WHERE id = $2",
        resolved_by, sanction_id,
    )


async def uphold_sanction(sanction_id: int, resolved_by: int):
    await execute(
        "UPDATE sanctions SET status = 'upheld', resolved_by = $1, resolved_at = now() WHERE id = $2",
        resolved_by, sanction_id,
    )


# ---------------------------------------------------------------------
# Prison
# ---------------------------------------------------------------------

async def create_prison_sentence(guild_id: int, user_id: int, sanction_id: int | None, given_by: int, saved_roles: str, ends_at) -> int:
    return await fetch_val(
        """INSERT INTO prison_sentences (guild_id, user_id, sanction_id, given_by, saved_roles, ends_at)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        guild_id, user_id, sanction_id, given_by, saved_roles, ends_at,
    )


async def get_active_prison_for_user(guild_id: int, user_id: int) -> dict | None:
    return await fetch_one(
        "SELECT * FROM prison_sentences WHERE guild_id = $1 AND user_id = $2 AND status = 'active'",
        guild_id, user_id,
    )


async def list_expired_prison_sentences() -> list[dict]:
    return await fetch_all(
        "SELECT * FROM prison_sentences WHERE status = 'active' AND ends_at <= now()"
    )


async def list_prison_sentences(guild_id: int, status: str | None = None) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM prison_sentences WHERE guild_id = $1 AND status = $2 ORDER BY created_at DESC",
            guild_id, status,
        )
    return await fetch_all("SELECT * FROM prison_sentences WHERE guild_id = $1 ORDER BY created_at DESC", guild_id)


async def release_prison_sentence(sentence_id: int):
    await execute(
        "UPDATE prison_sentences SET status = 'released', released_at = now() WHERE id = $1",
        sentence_id,
    )
