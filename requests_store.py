"""
Congés/absences, rapports d'incident, tickets — Postgres (Aiven).
"""

import logging

from database import fetch_one, fetch_all, execute, fetch_val

logger = logging.getLogger("RequestsStore")


# ---------------------------------------------------------------------
# Congés / absences
# ---------------------------------------------------------------------

async def create_leave_request(guild_id: int, user_id: int, start_date: str, end_date: str, reason: str) -> int:
    return await fetch_val(
        """INSERT INTO leave_requests (guild_id, user_id, start_date, end_date, reason)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        guild_id, user_id, start_date, end_date, reason,
    )


async def get_leave_request(leave_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM leave_requests WHERE id = $1", leave_id)


async def list_leave_requests(guild_id: int, status: str | None = None) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM leave_requests WHERE guild_id = $1 AND status = $2 ORDER BY created_at DESC",
            guild_id, status,
        )
    return await fetch_all("SELECT * FROM leave_requests WHERE guild_id = $1 ORDER BY created_at DESC", guild_id)


async def set_leave_status(leave_id: int, status: str, reviewed_by: int):
    await execute(
        "UPDATE leave_requests SET status = $1, reviewed_by = $2, reviewed_at = now() WHERE id = $3",
        status, reviewed_by, leave_id,
    )


# ---------------------------------------------------------------------
# Rapports d'incident (perturbation, excès de vitesse, etc.)
# ---------------------------------------------------------------------

async def create_incident_report(guild_id: int, user_id: int, incident_type: str, line_info: str, description: str) -> int:
    return await fetch_val(
        """INSERT INTO incident_reports (guild_id, user_id, incident_type, line_info, description)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        guild_id, user_id, incident_type, line_info, description,
    )


async def get_incident_report(incident_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM incident_reports WHERE id = $1", incident_id)


async def list_incident_reports(guild_id: int, status: str | None = None) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM incident_reports WHERE guild_id = $1 AND status = $2 ORDER BY created_at DESC",
            guild_id, status,
        )
    return await fetch_all("SELECT * FROM incident_reports WHERE guild_id = $1 ORDER BY created_at DESC", guild_id)


async def resolve_incident(incident_id: int, resolved_by: int, note: str):
    await execute(
        """UPDATE incident_reports SET status = 'resolved', resolved_by = $1,
           resolved_at = now(), resolution_note = $2 WHERE id = $3""",
        resolved_by, note, incident_id,
    )


async def dismiss_incident(incident_id: int, resolved_by: int, note: str):
    await execute(
        """UPDATE incident_reports SET status = 'dismissed', resolved_by = $1,
           resolved_at = now(), resolution_note = $2 WHERE id = $3""",
        resolved_by, note, incident_id,
    )


# ---------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------

async def create_ticket(guild_id: int, user_id: int, subject: str) -> int:
    return await fetch_val(
        "INSERT INTO tickets (guild_id, user_id, subject) VALUES ($1, $2, $3) RETURNING id",
        guild_id, user_id, subject,
    )


async def attach_ticket_channel(ticket_id: int, channel_id: int):
    await execute("UPDATE tickets SET channel_id = $1 WHERE id = $2", channel_id, ticket_id)


async def get_ticket(ticket_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM tickets WHERE id = $1", ticket_id)


async def get_ticket_by_channel(channel_id: int) -> dict | None:
    return await fetch_one("SELECT * FROM tickets WHERE channel_id = $1", channel_id)


async def list_tickets(guild_id: int, status: str | None = None) -> list[dict]:
    if status:
        return await fetch_all(
            "SELECT * FROM tickets WHERE guild_id = $1 AND status = $2 ORDER BY created_at DESC",
            guild_id, status,
        )
    return await fetch_all("SELECT * FROM tickets WHERE guild_id = $1 ORDER BY created_at DESC", guild_id)


async def close_ticket(ticket_id: int, closed_by: int):
    await execute(
        "UPDATE tickets SET status = 'closed', closed_by = $1, closed_at = now() WHERE id = $2",
        closed_by, ticket_id,
    )
