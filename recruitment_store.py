"""
Recrutement spontané — candidatures libres (hors offre publiée) et
gestion des employés (embauche, sanction, licenciement) — Postgres (Aiven).
"""

import logging

from database import fetch_one, fetch_all, execute, fetch_val

logger = logging.getLogger("RecruitmentStore")


# ---------------------------------------------------------------------
# Candidatures libres
# ---------------------------------------------------------------------

async def create_recruitment_application(guild_id: int, user_id: int, poste_souhaite: str,
                                           motivation: str, disponibilite: str) -> int | None:
    existing = await fetch_one(
        "SELECT id FROM recruitment_applications WHERE guild_id = $1 AND user_id = $2 AND status = 'pending'",
        guild_id, user_id,
    )
    if existing:
        return None
    app_id = await fetch_val(
        """INSERT INTO recruitment_applications (guild_id, user_id, poste_souhaite, motivation, disponibilite)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        guild_id, user_id, poste_souhaite, motivation, disponibilite,
    )
    return app_id


async def get_recruitment_application(app_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM recruitment_applications WHERE id = $1", app_id)


async def list_recruitment_applications(guild_id: int, status: str | None = None) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM recruitment_applications WHERE guild_id = $1 AND status = $2 ORDER BY created_at DESC",
            guild_id, status,
        )
    return await fetch_all(
        "SELECT * FROM recruitment_applications WHERE guild_id = $1 ORDER BY created_at DESC",
        guild_id,
    )


async def set_recruitment_status(app_id: int, status: str, handled_by: int):
    await execute(
        "UPDATE recruitment_applications SET status = $1, handled_by = $2, handled_at = now() WHERE id = $3",
        status, handled_by, app_id,
    )


# ---------------------------------------------------------------------
# Employés
# ---------------------------------------------------------------------

async def hire_employee(guild_id: int, user_id: int, role_id: int | None) -> int:
    existing = await fetch_one(
        "SELECT id FROM employees WHERE guild_id = $1 AND user_id = $2", guild_id, user_id
    )
    if existing:
        await execute(
            """UPDATE employees SET status = 'active', role_id = $1, hired_at = now(),
               fired_reason = NULL, fired_at = NULL, fired_by = NULL,
               sanction_reason = NULL, sanctioned_at = NULL, sanctioned_by = NULL
               WHERE id = $2""",
            role_id, existing["id"],
        )
        return existing["id"]
    emp_id = await fetch_val(
        "INSERT INTO employees (guild_id, user_id, role_id) VALUES ($1, $2, $3) RETURNING id",
        guild_id, user_id, role_id,
    )
    return emp_id


async def get_employee(guild_id: int, user_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM employees WHERE guild_id = $1 AND user_id = $2", guild_id, user_id)


async def list_employees(guild_id: int, status: str | None = None) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM employees WHERE guild_id = $1 AND status = $2 ORDER BY hired_at DESC",
            guild_id, status,
        )
    return await fetch_all("SELECT * FROM employees WHERE guild_id = $1 ORDER BY hired_at DESC", guild_id)


async def sanction_employee(guild_id: int, user_id: int, reason: str, sanctioned_by: int):
    await execute(
        """UPDATE employees SET status = 'sanctioned', sanction_reason = $1,
           sanctioned_at = now(), sanctioned_by = $2 WHERE guild_id = $3 AND user_id = $4""",
        reason, sanctioned_by, guild_id, user_id,
    )


async def fire_employee(guild_id: int, user_id: int, reason: str, fired_by: int):
    await execute(
        """UPDATE employees SET status = 'fired', fired_reason = $1,
           fired_at = now(), fired_by = $2 WHERE guild_id = $3 AND user_id = $4""",
        reason, fired_by, guild_id, user_id,
    )
