"""
API + Dashboard — FastAPI. Toute la gestion du bot (offres d'emploi,
candidatures, embauches, sanctions/licenciements, annonces avec image)
se pilote depuis le dashboard web. Le bot Discord ne fait plus que
publier les messages/embeds et gérer le bouton "Postuler".

Accès protégé par connexion Discord OAuth2 :
- Admin / Gérer le serveur : accès complet.
- Rôle RH configuré (hr_role_id) : accès aux offres, candidatures, employés.
"""

import os
import logging

from fastapi import FastAPI, HTTPException, Request, Depends, Form, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import discord

from config_store import get_guild_config, set_guild_config
from jobs_store import (
    create_job, attach_message, get_job, list_jobs, close_job, set_application_status,
)
from employees_store import hire_employee, list_employees, sanction_employee, fire_employee, get_employee
from requests_store import (
    list_leave_requests, get_leave_request, set_leave_status,
    list_incident_reports, get_incident_report, resolve_incident, dismiss_incident,
    list_tickets, get_ticket, close_ticket,
)
from sanctions_store import (
    create_sanction, attach_sanction_channel, get_sanction, list_sanctions,
    create_prison_sentence, get_active_prison_for_user, list_prison_sentences, release_prison_sentence,
)
import staff_auth
import datetime
import oauth

logger = logging.getLogger("API")

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
SECRET_KEY = os.getenv("SECRET_KEY", "change-moi-en-production")

app = FastAPI(title="Bus Admin Bot — Dashboard")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory=".")

bot_ref = {"bot": None}


def set_bot(bot):
    bot_ref["bot"] = bot


def get_guild():
    bot = bot_ref["bot"]
    if bot is None or not bot.is_ready():
        raise HTTPException(status_code=503, detail="Le bot n'est pas encore connecté.")
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        raise HTTPException(status_code=404, detail="Serveur introuvable (le bot n'y est peut-être pas).")
    return guild


# ---------------------------------------------------------------------
# Sérialisation des IDs Discord (snowflakes) en chaînes de texte.
# JavaScript perd en précision sur les grands entiers (>2^53), donc
# tout ID Discord renvoyé par l'API DOIT être une string, jamais un int.
# ---------------------------------------------------------------------

CONFIG_ID_FIELDS = [
    "rules_channel_id", "welcome_channel_id", "member_role_id",
    "jobs_channel_id", "application_log_channel_id",
    "employee_role_id", "hr_role_id",
    "employee_portal_channel_id", "hr_log_channel_id", "tickets_category_id",
    "sanction_category_id", "sanctioned_role_id", "prison_role_id",
]


def serialize_config(config: dict) -> dict:
    out = dict(config)
    for field in CONFIG_ID_FIELDS:
        if out.get(field) is not None:
            out[field] = str(out[field])
    return out


def serialize_job(job: dict) -> dict:
    out = dict(job)
    for field in ("id", "guild_id", "role_id", "posted_by", "channel_id", "message_id"):
        if out.get(field) is not None:
            out[field] = str(out[field])
    out["applications"] = [
        {**a, "user_id": str(a["user_id"])} for a in job.get("applications", [])
    ]
    return out


def serialize_employee(emp: dict) -> dict:
    out = dict(emp)
    for field in ("id", "guild_id", "user_id", "role_id", "sanctioned_by", "fired_by"):
        if out.get(field) is not None:
            out[field] = str(out[field])
    return out


# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------

def require_admin(request: Request):
    user = request.session.get("user")
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=401, detail="Non authentifié.")
    return user


async def require_hr(request: Request):
    """
    Autorise soit un admin/RH connecté via Discord OAuth2, soit un compte
    staff connecté via identifiant/mot de passe (base de données séparée).
    """
    staff_user = request.session.get("staff_user")
    if staff_user:
        return staff_user

    user = require_admin(request)
    guild = get_guild()
    member = guild.get_member(int(user["id"]))
    if member is None:
        raise HTTPException(status_code=403, detail="Membre introuvable sur le serveur.")

    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return user

    config = await get_guild_config(guild.id)
    hr_role_id = config.get("hr_role_id")
    if hr_role_id and any(r.id == hr_role_id for r in member.roles):
        return user

    raise HTTPException(status_code=403, detail="Rôle RH requis pour cette action.")


def require_staff_or_admin(request: Request):
    """Pour les pages qui acceptent staff OU admin, sans dépendre du cache Discord."""
    staff_user = request.session.get("staff_user")
    if staff_user:
        return staff_user
    return require_admin(request)


@app.get("/login")
async def login():
    return RedirectResponse(oauth.get_login_url())


@app.get("/callback")
async def callback(request: Request, code: str = None):
    if not code:
        raise HTTPException(status_code=400, detail="Code OAuth manquant.")
    token_data = await oauth.exchange_code(code)
    access_token = token_data["access_token"]
    user_info = await oauth.get_user_info(access_token)
    is_admin = await oauth.is_admin_on_target_guild(access_token)

    if not is_admin:
        return HTMLResponse(
            "<h1>Accès refusé</h1><p>Tu n'as pas les droits d'administration sur ce serveur.</p>",
            status_code=403,
        )

    request.session["user"] = {
        "id": user_info["id"],
        "username": user_info["username"],
        "avatar": user_info.get("avatar"),
        "is_admin": True,
    }
    return RedirectResponse("/")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login-page")


@app.get("/login-page", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


# ---------------------------------------------------------------------
# Auth — comptes STAFF (identifiant/mot de passe, BDD séparée)
# ---------------------------------------------------------------------

@app.get("/staff/login", response_class=HTMLResponse)
async def staff_login_page(request: Request):
    if not staff_auth.is_staff_enabled():
        return HTMLResponse("<h1>Le système de comptes staff n'est pas configuré (STAFF_DATABASE_URL manquant).</h1>", status_code=503)
    return templates.TemplateResponse(request, "staff_login.html", {})


@app.post("/staff/login")
async def staff_login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if not staff_auth.is_staff_enabled():
        raise HTTPException(status_code=503, detail="Système staff non configuré.")
    account = await staff_auth.verify_staff_login(username, password)
    if account is None:
        return templates.TemplateResponse(
            request, "staff_login.html", {"error": "Identifiant ou mot de passe incorrect."}, status_code=401
        )
    request.session["staff_user"] = {
        "id": str(-account["id"]),
        "username": account["display_name"],
        "is_staff": True,
    }
    return RedirectResponse("/staff", status_code=302)


@app.get("/staff/logout")
async def staff_logout(request: Request):
    request.session.pop("staff_user", None)
    return RedirectResponse("/staff/login")


@app.get("/staff", response_class=HTMLResponse)
async def staff_dashboard_page(request: Request):
    staff_user = request.session.get("staff_user")
    if not staff_user:
        return RedirectResponse("/staff/login")
    return templates.TemplateResponse(request, "staff_dashboard.html", {"user": staff_user, "guild_id": GUILD_ID})


# ---------------------------------------------------------------------
# API — gestion des comptes staff (admin uniquement)
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/staff_accounts")
async def api_list_staff_accounts(guild_id: int, user=Depends(require_admin)):
    if not staff_auth.is_staff_enabled():
        raise HTTPException(status_code=503, detail="Système staff non configuré (STAFF_DATABASE_URL manquant).")
    accounts = await staff_auth.list_staff_accounts(guild_id)
    return [
        {
            "id": str(a["id"]),
            "username": a["username"],
            "display_name": a["display_name"],
            "active": a["active"],
            "created_at": a["created_at"].isoformat() if a["created_at"] else None,
            "last_login_at": a["last_login_at"].isoformat() if a["last_login_at"] else None,
        }
        for a in accounts
    ]


@app.post("/api/{guild_id}/staff_accounts")
async def api_create_staff_account(guild_id: int, request: Request, user=Depends(require_admin)):
    if not staff_auth.is_staff_enabled():
        raise HTTPException(status_code=503, detail="Système staff non configuré (STAFF_DATABASE_URL manquant).")
    body = await request.json()
    username = (body.get("username") or "").strip()
    display_name = (body.get("display_name") or username).strip()
    if not username:
        raise HTTPException(status_code=400, detail="Identifiant requis.")

    existing = await staff_auth.get_staff_account_by_username(username)
    if existing:
        raise HTTPException(status_code=400, detail="Cet identifiant existe déjà.")

    password = body.get("password") or staff_auth.generate_password()
    account_id = await staff_auth.create_staff_account(guild_id, username, display_name, password, int(user["id"]))
    return {"id": str(account_id), "username": username.lower(), "password": password}


@app.post("/api/{guild_id}/staff_accounts/{account_id}/toggle")
async def api_toggle_staff_account(guild_id: int, account_id: int, request: Request, user=Depends(require_admin)):
    if not staff_auth.is_staff_enabled():
        raise HTTPException(status_code=503, detail="Système staff non configuré.")
    body = await request.json()
    await staff_auth.set_staff_active(account_id, bool(body.get("active", True)))
    return {"status": "ok"}


@app.post("/api/{guild_id}/staff_accounts/{account_id}/reset_password")
async def api_reset_staff_password(guild_id: int, account_id: int, user=Depends(require_admin)):
    if not staff_auth.is_staff_enabled():
        raise HTTPException(status_code=503, detail="Système staff non configuré.")
    new_password = staff_auth.generate_password()
    await staff_auth.reset_staff_password(account_id, new_password)
    return {"password": new_password}


@app.delete("/api/{guild_id}/staff_accounts/{account_id}")
async def api_delete_staff_account(guild_id: int, account_id: int, user=Depends(require_admin)):
    if not staff_auth.is_staff_enabled():
        raise HTTPException(status_code=503, detail="Système staff non configuré.")
    await staff_auth.delete_staff_account(account_id, guild_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    user = request.session.get("user")
    if not user or not user.get("is_admin"):
        return RedirectResponse("/login-page")
    return templates.TemplateResponse(request, "dashboard.html", {"user": user, "guild_id": GUILD_ID})


# ---------------------------------------------------------------------
# API — santé
# ---------------------------------------------------------------------

@app.get("/api/health")
async def health():
    return {"status": "ok", "bot_ready": bot_ref["bot"] is not None and bot_ref["bot"].is_ready()}


# ---------------------------------------------------------------------
# API — stats & membres
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/stats")
async def guild_stats(guild_id: int, user=Depends(require_admin)):
    guild = get_guild()
    bots_count = sum(1 for m in guild.members if m.bot)
    return {
        "name": guild.name,
        "members_total": guild.member_count,
        "humans": guild.member_count - bots_count,
        "bots": bots_count,
        "channels": len(guild.channels),
        "roles": len(guild.roles),
    }


@app.get("/api/{guild_id}/members")
async def guild_members(guild_id: int, limit: int = 100, user=Depends(require_admin)):
    guild = get_guild()
    members = list(guild.members)[:limit]
    return [
        {
            "id": str(m.id),
            "name": str(m),
            "avatar": m.display_avatar.url,
            "roles": [r.name for r in m.roles if r.name != "@everyone"],
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
        }
        for m in members
    ]


# ---------------------------------------------------------------------
# API — config
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/config")
async def read_config(guild_id: int, user=Depends(require_admin)):
    return serialize_config(await get_guild_config(guild_id))


@app.get("/api/{guild_id}/channels")
async def list_channels(guild_id: int, user=Depends(require_admin)):
    guild = get_guild()
    return [{"id": str(c.id), "name": c.name, "type": str(c.type)} for c in guild.text_channels]


@app.get("/api/{guild_id}/roles")
async def list_roles(guild_id: int, user=Depends(require_admin)):
    guild = get_guild()
    return [{"id": str(r.id), "name": r.name} for r in guild.roles if r.name != "@everyone"]


@app.post("/api/{guild_id}/config")
async def update_config(guild_id: int, request: Request, user=Depends(require_admin)):
    body = await request.json()
    allowed_fields = {
        "rules_channel_id", "welcome_channel_id", "member_role_id",
        "jobs_channel_id", "application_log_channel_id",
        "employee_role_id", "hr_role_id",
        "employee_portal_channel_id", "hr_log_channel_id", "tickets_category_id",
        "sanction_category_id", "sanctioned_role_id", "prison_role_id",
    }
    fields = {k: int(v) for k, v in body.items() if k in allowed_fields and v}
    if not fields:
        raise HTTPException(status_code=400, detail="Aucun champ valide fourni.")
    await set_guild_config(guild_id, **fields)
    return serialize_config(await get_guild_config(guild_id))


# ---------------------------------------------------------------------
# API — offres d'emploi (création, liste, clôture — tout depuis le dashboard)
# ---------------------------------------------------------------------


async def resolve_channel(guild, channel_id: int):
    """Cherche le salon en cache, sinon va le chercher directement via l'API Discord."""
    channel = guild.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        return await guild.fetch_channel(int(channel_id))
    except (discord.NotFound, discord.Forbidden):
        return None

def job_embed_dict(job: dict) -> discord.Embed:
    color = discord.Color.green() if job["status"] == "open" else discord.Color.greyple()
    embed = discord.Embed(title=f"🚌 {job['title']}", description=job["description"], color=color)
    embed.add_field(name="Statut", value="🟢 Ouvert" if job["status"] == "open" else "🔴 Clôturé", inline=True)
    embed.add_field(name="Candidatures", value=str(len(job["applications"])), inline=True)
    embed.set_footer(text=f"Offre #{job['id']}")
    return embed


@app.get("/api/{guild_id}/jobs")
async def api_list_jobs(guild_id: int, status: str = None, user=Depends(require_hr)):
    jobs = await list_jobs(guild_id, status=status)
    return [serialize_job(j) for j in jobs]


@app.post("/api/{guild_id}/jobs")
async def api_create_job(guild_id: int, request: Request, user=Depends(require_hr)):
    """
    Body JSON : { "title": "...", "description": "...", "role_id": "123" (optionnel) }
    Publie automatiquement l'offre dans le salon configuré (jobs_channel_id).
    """
    body = await request.json()
    title = (body.get("title") or "").strip()
    description = (body.get("description") or "").strip()
    role_id = body.get("role_id")
    role_id = int(role_id) if role_id else None

    if not title or not description:
        raise HTTPException(status_code=400, detail="Titre et description requis.")

    guild = get_guild()
    config = await get_guild_config(guild_id)
    channel_id = config.get("jobs_channel_id")
    if not channel_id:
        raise HTTPException(status_code=400, detail="Aucun salon d'offres configuré (voir l'onglet Configuration).")
    channel = await resolve_channel(guild, channel_id)
    if channel is None:
        raise HTTPException(
            status_code=400,
            detail=f"Le salon configuré (ID {channel_id}) est introuvable ou inaccessible au bot. Vérifie qu'il existe toujours et que le bot y a accès."
        )

    job_id = await create_job(guild_id, title, description, role_id, int(user["id"]))
    job = await get_job(job_id)

    from jobs import JobApplyButton  # import tardif pour éviter les cycles
    view = discord.ui.View(timeout=None)
    view.add_item(JobApplyButton(job_id))
    message = await channel.send(embed=job_embed_dict(job), view=view)
    await attach_message(job_id, channel.id, message.id)

    return serialize_job(await get_job(job_id))


@app.get("/api/{guild_id}/jobs/{job_id}")
async def api_get_job(guild_id: int, job_id: int, user=Depends(require_hr)):
    job = await get_job(job_id)
    if job is None or job["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Offre introuvable.")
    return serialize_job(job)


@app.post("/api/{guild_id}/jobs/{job_id}/close")
async def api_close_job(guild_id: int, job_id: int, user=Depends(require_hr)):
    job = await get_job(job_id)
    if job is None or job["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Offre introuvable.")
    await close_job(job_id)

    if job["channel_id"] and job["message_id"]:
        guild = get_guild()
        channel = guild.get_channel(job["channel_id"])
        if channel:
            try:
                message = await channel.fetch_message(job["message_id"])
                updated = await get_job(job_id)
                await message.edit(embed=job_embed_dict(updated), view=None)
            except discord.NotFound:
                pass

    return serialize_job(await get_job(job_id))


# ---------------------------------------------------------------------
# API — candidatures (fiche complète du candidat + accepter/refuser)
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/jobs/{job_id}/applications")
async def api_job_applications(guild_id: int, job_id: int, user=Depends(require_hr)):
    """Renvoie chaque candidature enrichie avec le profil Discord complet du candidat."""
    job = await get_job(job_id)
    if job is None or job["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Offre introuvable.")

    guild = get_guild()
    enriched = []
    for a in job["applications"]:
        member = guild.get_member(a["user_id"])
        enriched.append({
            **a,
            "user_id": str(a["user_id"]),
            "job_id": str(job_id),
            "job_title": job["title"],
            "username": str(member) if member else f"Utilisateur {a['user_id']}",
            "avatar": member.display_avatar.url if member else None,
            "account_created_at": member.created_at.isoformat() if member else None,
            "joined_at": member.joined_at.isoformat() if member and member.joined_at else None,
            "roles": [r.name for r in member.roles if r.name != "@everyone"] if member else [],
            "in_guild": member is not None,
        })
    return enriched


@app.post("/api/{guild_id}/jobs/{job_id}/applications/{user_id}/accept")
async def api_accept_application(guild_id: int, job_id: int, user_id: int, user=Depends(require_hr)):
    job = await get_job(job_id)
    if job is None or job["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Offre introuvable.")

    guild = get_guild()
    member = guild.get_member(user_id)

    if job.get("role_id") and member:
        role = guild.get_role(job["role_id"])
        if role:
            try:
                await member.add_roles(role, reason=f"Candidature acceptée — {job['title']}")
            except discord.Forbidden:
                pass

    await set_application_status(job_id, user_id, "accepted")
    await hire_employee(guild_id, user_id, job.get("role_id"))

    if member:
        try:
            await member.send(f"🎉 Ta candidature pour **{job['title']}** a été **acceptée** sur **{guild.name}** !")
        except discord.Forbidden:
            pass

    return {"status": "ok"}


@app.post("/api/{guild_id}/jobs/{job_id}/applications/{user_id}/deny")
async def api_deny_application(guild_id: int, job_id: int, user_id: int, user=Depends(require_hr)):
    job = await get_job(job_id)
    if job is None or job["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Offre introuvable.")

    await set_application_status(job_id, user_id, "refused")

    guild = get_guild()
    member = guild.get_member(user_id)
    if member:
        try:
            await member.send(f"❌ Ta candidature pour **{job['title']}** n'a pas été retenue sur **{guild.name}**.")
        except discord.Forbidden:
            pass

    return {"status": "ok"}


# ---------------------------------------------------------------------
# API — employés (sanctionner / virer depuis le dashboard)
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/employees")
async def api_list_employees(guild_id: int, status: str = None, user=Depends(require_hr)):
    employees = await list_employees(guild_id, status=status)
    guild = get_guild()
    enriched = []
    for e in employees:
        member = guild.get_member(e["user_id"])
        enriched.append({
            **serialize_employee(e),
            "username": str(member) if member else f"Utilisateur {e['user_id']}",
            "avatar": member.display_avatar.url if member else None,
        })
    return enriched


@app.post("/api/{guild_id}/employees/{user_id}/sanction")
async def api_sanction_employee(guild_id: int, user_id: int, request: Request, user=Depends(require_hr)):
    """
    Sanctionne un employé : lui donne le rôle "Sanctionné" (accès à la
    catégorie tribunal), crée un salon privé dédié avec le motif et un
    bouton "Contester". Ne vire pas l'employé.
    """
    body = await request.json()
    reason = (body.get("reason") or "Non précisée").strip()
    guild = get_guild()
    member = guild.get_member(user_id)

    await sanction_employee(guild_id, user_id, reason, int(user["id"]))
    sanction_id = await create_sanction(guild_id, user_id, reason, int(user["id"]))

    config = await get_guild_config(guild_id)
    sanctioned_role_id = config.get("sanctioned_role_id")
    category_id = config.get("sanction_category_id")

    if member and sanctioned_role_id:
        role = guild.get_role(sanctioned_role_id)
        if role:
            try:
                await member.add_roles(role, reason=f"Sanction #{sanction_id}")
            except discord.Forbidden:
                pass

    channel = None
    if member:
        category = await resolve_channel(guild, category_id) if category_id else None
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        hr_role_id = config.get("hr_role_id")
        if hr_role_id:
            hr_role = guild.get_role(hr_role_id)
            if hr_role:
                overwrites[hr_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        try:
            channel = await guild.create_text_channel(
                f"sanction-{sanction_id}-{member.name}"[:95],
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites,
                topic=f"Sanction #{sanction_id}",
            )
        except discord.Forbidden:
            channel = None

    if channel:
        from tribunal import ContestButton
        embed = discord.Embed(
            title=f"⚖️ Sanction #{sanction_id}",
            description=f"{member.mention} a été sanctionné.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Raison", value=reason, inline=False)
        embed.set_footer(text=f"Sanctionné par {user['username']}")
        view = discord.ui.View(timeout=None)
        view.add_item(ContestButton(sanction_id))
        message = await channel.send(content=member.mention, embed=embed, view=view)
        await attach_sanction_channel(sanction_id, channel.id, message.id)

    if member:
        try:
            await member.send(f"⚠️ Tu as été sanctionné sur **{guild.name}** : {reason}" + (f"\nVoir {channel.mention}" if channel else ""))
        except discord.Forbidden:
            pass

    return {"status": "ok", "sanction_id": str(sanction_id)}


@app.post("/api/{guild_id}/employees/{user_id}/fire")
async def api_fire_employee(guild_id: int, user_id: int, request: Request, user=Depends(require_hr)):
    body = await request.json()
    reason = (body.get("reason") or "Non précisée").strip()
    guild = get_guild()
    member = guild.get_member(user_id)

    config = await get_guild_config(guild_id)
    role_id = config.get("employee_role_id")
    if member and role_id:
        role = guild.get_role(role_id)
        if role and role in member.roles:
            try:
                await member.remove_roles(role, reason=f"Licenciement (dashboard) : {reason}")
            except discord.Forbidden:
                pass

    await fire_employee(guild_id, user_id, reason, int(user["id"]))
    if member:
        try:
            await member.send(f"🚪 Tu as été licencié de **{guild.name}** : {reason}")
        except discord.Forbidden:
            pass
    return {"status": "ok"}


# ---------------------------------------------------------------------
# API — sanctions (liste) & Prison (mise en prison / libération)
# ---------------------------------------------------------------------

def serialize_sanction(s: dict) -> dict:
    out = dict(s)
    for field in ("id", "guild_id", "user_id", "sanctioned_by", "channel_id", "message_id", "resolved_by"):
        if out.get(field) is not None:
            out[field] = str(out[field])
    return out


@app.get("/api/{guild_id}/sanctions")
async def api_list_sanctions(guild_id: int, status: str = None, user=Depends(require_hr)):
    sanctions = await list_sanctions(guild_id, status=status)
    guild = get_guild()
    enriched = []
    for s in sanctions:
        member = guild.get_member(s["user_id"])
        enriched.append({
            **serialize_sanction(s),
            "username": str(member) if member else f"Utilisateur {s['user_id']}",
            "avatar": member.display_avatar.url if member else None,
        })
    return enriched


def serialize_prison(p: dict) -> dict:
    out = dict(p)
    for field in ("id", "guild_id", "user_id", "sanction_id", "given_by"):
        if out.get(field) is not None:
            out[field] = str(out[field])
    return out


@app.get("/api/{guild_id}/prison")
async def api_list_prison(guild_id: int, status: str = None, user=Depends(require_hr)):
    sentences = await list_prison_sentences(guild_id, status=status)
    guild = get_guild()
    enriched = []
    for p in sentences:
        member = guild.get_member(p["user_id"])
        enriched.append({
            **serialize_prison(p),
            "username": str(member) if member else f"Utilisateur {p['user_id']}",
            "avatar": member.display_avatar.url if member else None,
        })
    return enriched


@app.post("/api/{guild_id}/employees/{user_id}/imprison")
async def api_imprison_employee(guild_id: int, user_id: int, request: Request, user=Depends(require_hr)):
    """
    [Admin uniquement] Envoie le membre en Prison pour une durée donnée
    (en heures). Sauvegarde tous ses rôles actuels, les retire, donne
    le rôle Prison. Un job de fond restaure tout à la fin de la peine.
    """
    body = await request.json()
    hours = body.get("hours")
    sanction_id = body.get("sanction_id")
    if not hours or float(hours) <= 0:
        raise HTTPException(status_code=400, detail="Durée de prison invalide.")

    guild = get_guild()
    member = guild.get_member(user_id)
    if member is None:
        raise HTTPException(status_code=404, detail="Membre introuvable sur le serveur.")

    existing = await get_active_prison_for_user(guild_id, user_id)
    if existing:
        raise HTTPException(status_code=400, detail="Ce membre est déjà en prison.")

    config = await get_guild_config(guild_id)
    prison_role_id = config.get("prison_role_id")
    if not prison_role_id:
        raise HTTPException(status_code=400, detail="Rôle Prison non configuré (voir l'onglet Configuration).")
    prison_role = guild.get_role(prison_role_id)
    if prison_role is None:
        raise HTTPException(status_code=400, detail="Rôle Prison introuvable sur le serveur.")

    current_role_ids = [r.id for r in member.roles if r.name != "@everyone" and r.id != prison_role_id]
    saved_roles = ",".join(str(rid) for rid in current_role_ids)
    ends_at = datetime.datetime.utcnow() + datetime.timedelta(hours=float(hours))

    sentence_id = await create_prison_sentence(
        guild_id, user_id, int(sanction_id) if sanction_id else None, int(user["id"]), saved_roles, ends_at
    )

    try:
        roles_to_remove = [guild.get_role(rid) for rid in current_role_ids]
        roles_to_remove = [r for r in roles_to_remove if r is not None]
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason=f"Mise en prison #{sentence_id}")
        await member.add_roles(prison_role, reason=f"Mise en prison #{sentence_id}")
    except discord.Forbidden:
        raise HTTPException(status_code=403, detail="Le bot n'a pas la permission de gérer les rôles de ce membre.")

    try:
        await member.send(f"🔒 Tu as été envoyé en **prison** sur **{guild.name}** pour {hours}h.")
    except discord.Forbidden:
        pass

    return {"status": "ok", "sentence_id": str(sentence_id), "ends_at": ends_at.isoformat()}


@app.post("/api/{guild_id}/prison/{sentence_id}/release")
async def api_release_prison(guild_id: int, sentence_id: int, user=Depends(require_hr)):
    """[Admin uniquement] Libère un membre avant la fin de sa peine, restaure ses rôles."""
    sentences = await list_prison_sentences(guild_id, status="active")
    sentence = next((s for s in sentences if s["id"] == sentence_id), None)
    if sentence is None:
        raise HTTPException(status_code=404, detail="Peine introuvable ou déjà terminée.")

    guild = get_guild()
    member = guild.get_member(sentence["user_id"])
    config = await get_guild_config(guild_id)
    prison_role_id = config.get("prison_role_id")

    if member:
        if prison_role_id:
            prison_role = guild.get_role(prison_role_id)
            if prison_role and prison_role in member.roles:
                try:
                    await member.remove_roles(prison_role, reason="Libération anticipée")
                except discord.Forbidden:
                    pass
        saved_ids = [int(rid) for rid in sentence["saved_roles"].split(",") if rid]
        roles_to_restore = [guild.get_role(rid) for rid in saved_ids]
        roles_to_restore = [r for r in roles_to_restore if r is not None]
        if roles_to_restore:
            try:
                await member.add_roles(*roles_to_restore, reason="Libération anticipée — restauration des rôles")
            except discord.Forbidden:
                pass
        try:
            await member.send(f"🔓 Tu as été libéré de prison sur **{guild.name}** (libération anticipée).")
        except discord.Forbidden:
            pass

    await release_prison_sentence(sentence_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------
# API — catégories (pour la config des tickets)
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/categories")
async def list_categories(guild_id: int, user=Depends(require_admin)):
    guild = get_guild()
    return [{"id": str(c.id), "name": c.name} for c in guild.categories]


# ---------------------------------------------------------------------
# API — congés / absences
# ---------------------------------------------------------------------

def serialize_leave(leave: dict) -> dict:
    out = dict(leave)
    out["id"] = str(out["id"])
    out["user_id"] = str(out["user_id"])
    out["guild_id"] = str(out["guild_id"])
    if out.get("reviewed_by") is not None:
        out["reviewed_by"] = str(out["reviewed_by"])
    return out


@app.get("/api/{guild_id}/leave_requests")
async def api_list_leave_requests(guild_id: int, status: str = None, user=Depends(require_hr)):
    requests_ = await list_leave_requests(guild_id, status=status)
    guild = get_guild()
    enriched = []
    for r in requests_:
        member = guild.get_member(r["user_id"])
        enriched.append({
            **serialize_leave(r),
            "username": str(member) if member else f"Utilisateur {r['user_id']}",
            "avatar": member.display_avatar.url if member else None,
        })
    return enriched


@app.post("/api/{guild_id}/leave_requests/{leave_id}/accept")
async def api_accept_leave(guild_id: int, leave_id: int, user=Depends(require_hr)):
    leave = await get_leave_request(leave_id)
    if leave is None or leave["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Demande introuvable.")
    await set_leave_status(leave_id, "accepted", int(user["id"]))
    guild = get_guild()
    member = guild.get_member(leave["user_id"])
    if member:
        try:
            await member.send(f"✅ Ta demande de congé du {leave['start_date']} au {leave['end_date']} a été **acceptée**.")
        except discord.Forbidden:
            pass
    return {"status": "ok"}


@app.post("/api/{guild_id}/leave_requests/{leave_id}/deny")
async def api_deny_leave(guild_id: int, leave_id: int, user=Depends(require_hr)):
    leave = await get_leave_request(leave_id)
    if leave is None or leave["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Demande introuvable.")
    await set_leave_status(leave_id, "refused", int(user["id"]))
    guild = get_guild()
    member = guild.get_member(leave["user_id"])
    if member:
        try:
            await member.send(f"❌ Ta demande de congé du {leave['start_date']} au {leave['end_date']} a été **refusée**.")
        except discord.Forbidden:
            pass
    return {"status": "ok"}


# ---------------------------------------------------------------------
# API — rapports d'incident
# ---------------------------------------------------------------------

def serialize_incident(incident: dict) -> dict:
    out = dict(incident)
    out["id"] = str(out["id"])
    out["user_id"] = str(out["user_id"])
    out["guild_id"] = str(out["guild_id"])
    if out.get("resolved_by") is not None:
        out["resolved_by"] = str(out["resolved_by"])
    return out


@app.get("/api/{guild_id}/incidents")
async def api_list_incidents(guild_id: int, status: str = None, user=Depends(require_hr)):
    incidents = await list_incident_reports(guild_id, status=status)
    guild = get_guild()
    enriched = []
    for i in incidents:
        member = guild.get_member(i["user_id"])
        enriched.append({
            **serialize_incident(i),
            "username": str(member) if member else f"Utilisateur {i['user_id']}",
            "avatar": member.display_avatar.url if member else None,
        })
    return enriched


@app.post("/api/{guild_id}/incidents/{incident_id}/resolve")
async def api_resolve_incident(guild_id: int, incident_id: int, request: Request, user=Depends(require_hr)):
    incident = await get_incident_report(incident_id)
    if incident is None or incident["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Rapport introuvable.")
    body = await request.json()
    note = (body.get("note") or "").strip()
    await resolve_incident(incident_id, int(user["id"]), note)
    return {"status": "ok"}


@app.post("/api/{guild_id}/incidents/{incident_id}/dismiss")
async def api_dismiss_incident(guild_id: int, incident_id: int, request: Request, user=Depends(require_hr)):
    incident = await get_incident_report(incident_id)
    if incident is None or incident["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Rapport introuvable.")
    body = await request.json()
    note = (body.get("note") or "").strip()
    await dismiss_incident(incident_id, int(user["id"]), note)
    return {"status": "ok"}


# ---------------------------------------------------------------------
# API — tickets
# ---------------------------------------------------------------------

def serialize_ticket(ticket: dict) -> dict:
    out = dict(ticket)
    out["id"] = str(out["id"])
    out["user_id"] = str(out["user_id"])
    out["guild_id"] = str(out["guild_id"])
    if out.get("channel_id") is not None:
        out["channel_id"] = str(out["channel_id"])
    if out.get("closed_by") is not None:
        out["closed_by"] = str(out["closed_by"])
    return out


@app.get("/api/{guild_id}/tickets")
async def api_list_tickets(guild_id: int, status: str = None, user=Depends(require_hr)):
    tickets = await list_tickets(guild_id, status=status)
    guild = get_guild()
    enriched = []
    for t in tickets:
        member = guild.get_member(t["user_id"])
        channel = guild.get_channel(t["channel_id"]) if t.get("channel_id") else None
        enriched.append({
            **serialize_ticket(t),
            "username": str(member) if member else f"Utilisateur {t['user_id']}",
            "avatar": member.display_avatar.url if member else None,
            "channel_name": channel.name if channel else None,
        })
    return enriched


@app.post("/api/{guild_id}/tickets/{ticket_id}/close")
async def api_close_ticket(guild_id: int, ticket_id: int, user=Depends(require_hr)):
    ticket = await get_ticket(ticket_id)
    if ticket is None or ticket["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Ticket introuvable.")
    await close_ticket(ticket_id, int(user["id"]))

    if ticket.get("channel_id"):
        guild = get_guild()
        channel = guild.get_channel(ticket["channel_id"])
        if channel:
            try:
                await channel.send("🔒 Ce ticket a été fermé depuis le dashboard.")
                await channel.edit(name=f"closed-{channel.name}"[:95])
            except discord.Forbidden:
                pass

    return {"status": "ok"}


# ---------------------------------------------------------------------
# API — messages personnalisés / annonces (avec image en pièce jointe)
# ---------------------------------------------------------------------

COLOR_MAP = {
    "blurple": discord.Color.blurple(),
    "green": discord.Color.green(),
    "red": discord.Color.red(),
    "orange": discord.Color.orange(),
    "gold": discord.Color.gold(),
    "greyple": discord.Color.greyple(),
}


@app.post("/api/{guild_id}/send_message")
async def send_message(
    guild_id: int,
    channel_id: str = Form(...),
    mode: str = Form("embed"),
    title: str = Form(""),
    content: str = Form(...),
    color: str = Form("blurple"),
    mention_everyone: bool = Form(False),
    image: UploadFile | None = File(None),
    user=Depends(require_admin),
):
    guild = get_guild()
    channel = await resolve_channel(guild, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail=f"Salon introuvable ou inaccessible (ID {channel_id}).")

    content = content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Le contenu du message est vide.")

    prefix = "@everyone " if mention_everyone else ""
    file_obj = None
    if image is not None and image.filename:
        image_bytes = await image.read()
        file_obj = discord.File(fp=__import__("io").BytesIO(image_bytes), filename=image.filename)

    try:
        if mode == "plain":
            await channel.send(
                content=f"{prefix}{content}"[:2000],
                file=file_obj if file_obj else None,
            )
        else:
            embed = discord.Embed(
                title=title.strip() or None,
                description=content[:4000],
                color=COLOR_MAP.get(color, discord.Color.blurple()),
            )
            embed.set_footer(text=f"Message envoyé par {user['username']} via le dashboard")
            if file_obj:
                embed.set_image(url=f"attachment://{file_obj.filename}")
            await channel.send(
                content=prefix if mention_everyone else None,
                embed=embed,
                file=file_obj if file_obj else None,
            )
    except discord.Forbidden:
        raise HTTPException(status_code=403, detail="Le bot n'a pas la permission d'écrire dans ce salon.")

    return {"status": "ok", "channel_id": channel_id}
