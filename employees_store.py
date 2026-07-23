"""
Gestion des employés — Postgres (Aiven).
L'embauche se fait désormais via l'acceptation d'une candidature liée à une
offre d'emploi (jobs_store), ou manuellement depuis le dashboard.
"""

import logging

from database import fetch_one, fetch_all, execute, fetch_val

logger = logging.getLogger("EmployeesStore")


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
