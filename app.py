"""
API + Dashboard — FastAPI, sans BDD pour la config live (config JSON en base,
jobs/recrutement/employés en Postgres, cache discord.py du bot en direct).

Accès protégé par connexion Discord OAuth2. Deux niveaux :
- Admin / Gérer le serveur : accès complet au dashboard.
- Rôle RH configuré (hr_role_id) : accès aux offres, recrutement, employés
  (accepter/refuser candidatures, sanctionner, virer) sans être admin.
"""

import os
import logging

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import discord

from config_store import get_guild_config, set_guild_config
from jobs_store import list_jobs, get_job, close_job
from recruitment_store import (
    list_recruitment_applications, get_recruitment_application, set_recruitment_status,
    hire_employee, list_employees, sanction_employee, fire_employee,
)
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
# Auth
# ---------------------------------------------------------------------

def require_admin(request: Request):
    user = request.session.get("user")
    if not user or not user.get("is_admin"):
        raise HTTPException(status_code=401, detail="Non authentifié.")
    return user


async def require_hr(request: Request):
    """Autorise les admins/Gérer le serveur (déjà filtrés à la connexion) OU
    les membres ayant le rôle RH configuré (hr_role_id) sur le serveur live."""
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
            "id": m.id,
            "name": str(m),
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
    return await get_guild_config(guild_id)


@app.get("/api/{guild_id}/channels")
async def list_channels(guild_id: int, user=Depends(require_admin)):
    guild = get_guild()
    return [{"id": c.id, "name": c.name, "type": str(c.type)} for c in guild.text_channels]


@app.get("/api/{guild_id}/roles")
async def list_roles(guild_id: int, user=Depends(require_admin)):
    guild = get_guild()
    return [{"id": r.id, "name": r.name} for r in guild.roles if r.name != "@everyone"]


@app.post("/api/{guild_id}/config")
async def update_config(guild_id: int, request: Request, user=Depends(require_admin)):
    body = await request.json()
    allowed_fields = {
        "rules_channel_id", "welcome_channel_id", "member_role_id",
        "jobs_channel_id", "application_log_channel_id",
        "recruitment_channel_id", "recruitment_log_channel_id",
        "employee_role_id", "hr_role_id",
    }
    fields = {k: int(v) for k, v in body.items() if k in allowed_fields and v}
    if not fields:
        raise HTTPException(status_code=400, detail="Aucun champ valide fourni.")
    await set_guild_config(guild_id, **fields)
    return await get_guild_config(guild_id)


# ---------------------------------------------------------------------
# API — offres d'emploi
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/jobs")
async def api_list_jobs(guild_id: int, status: str = None, user=Depends(require_hr)):
    return await list_jobs(guild_id, status=status)


@app.get("/api/{guild_id}/jobs/{job_id}")
async def api_get_job(guild_id: int, job_id: int, user=Depends(require_hr)):
    job = await get_job(job_id)
    if job is None or job["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Offre introuvable.")
    return job


@app.post("/api/{guild_id}/jobs/{job_id}/close")
async def api_close_job(guild_id: int, job_id: int, user=Depends(require_hr)):
    job = await get_job(job_id)
    if job is None or job["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Offre introuvable.")
    await close_job(job_id)
    return await get_job(job_id)


# ---------------------------------------------------------------------
# API — recrutement spontané (accepter / refuser depuis le dashboard)
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/recruitment")
async def api_list_recruitment(guild_id: int, status: str = None, user=Depends(require_hr)):
    return await list_recruitment_applications(guild_id, status=status)


async def _dm_and_maybe_role(guild, application: dict, accept: bool, hr_user_id: int):
    member = guild.get_member(application["user_id"])
    if accept:
        config = await get_guild_config(guild.id)
        role_id = config.get("employee_role_id")
        if member and role_id:
            role = guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Candidature spontanée acceptée (dashboard)")
                except discord.Forbidden:
                    pass
        await hire_employee(guild.id, application["user_id"], role_id)
        if member:
            try:
                await member.send(f"🎉 Ta candidature a été **acceptée** sur **{guild.name}** ! Bienvenue dans l'équipe.")
            except discord.Forbidden:
                pass
    else:
        if member:
            try:
                await member.send(f"❌ Ta candidature sur **{guild.name}** n'a pas été retenue.")
            except discord.Forbidden:
                pass


@app.post("/api/{guild_id}/recruitment/{app_id}/accept")
async def api_accept_recruitment(guild_id: int, app_id: int, user=Depends(require_hr)):
    application = await get_recruitment_application(app_id)
    if application is None or application["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Candidature introuvable.")
    guild = get_guild()
    await _dm_and_maybe_role(guild, application, accept=True, hr_user_id=int(user["id"]))
    await set_recruitment_status(app_id, "accepted", int(user["id"]))
    return await get_recruitment_application(app_id)


@app.post("/api/{guild_id}/recruitment/{app_id}/deny")
async def api_deny_recruitment(guild_id: int, app_id: int, user=Depends(require_hr)):
    application = await get_recruitment_application(app_id)
    if application is None or application["guild_id"] != guild_id:
        raise HTTPException(status_code=404, detail="Candidature introuvable.")
    guild = get_guild()
    await _dm_and_maybe_role(guild, application, accept=False, hr_user_id=int(user["id"]))
    await set_recruitment_status(app_id, "refused", int(user["id"]))
    return await get_recruitment_application(app_id)


# ---------------------------------------------------------------------
# API — employés (sanctionner / virer depuis le dashboard)
# ---------------------------------------------------------------------

@app.get("/api/{guild_id}/employees")
async def api_list_employees(guild_id: int, status: str = None, user=Depends(require_hr)):
    return await list_employees(guild_id, status=status)


@app.post("/api/{guild_id}/employees/{user_id}/sanction")
async def api_sanction_employee(guild_id: int, user_id: int, request: Request, user=Depends(require_hr)):
    body = await request.json()
    reason = (body.get("reason") or "Non précisée").strip()
    guild = get_guild()
    member = guild.get_member(user_id)
    await sanction_employee(guild_id, user_id, reason, int(user["id"]))
    if member:
        try:
            await member.send(f"⚠️ Tu as été sanctionné sur **{guild.name}** : {reason}")
        except discord.Forbidden:
            pass
    return {"status": "ok"}


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
# API — messages personnalisés
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
async def send_message(guild_id: int, request: Request, user=Depends(require_admin)):
    body = await request.json()
    channel_id = body.get("channel_id")
    mode = body.get("mode", "embed")
    content = (body.get("content") or "").strip()
    title = (body.get("title") or "").strip()
    color_key = body.get("color", "blurple")
    mention_everyone = bool(body.get("mention_everyone", False))

    if not channel_id:
        raise HTTPException(status_code=400, detail="channel_id manquant.")
    if not content:
        raise HTTPException(status_code=400, detail="Le contenu du message est vide.")

    guild = get_guild()
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        raise HTTPException(status_code=404, detail="Salon introuvable.")

    prefix = "@everyone " if mention_everyone else ""

    try:
        if mode == "plain":
            await channel.send(content=f"{prefix}{content}"[:2000])
        else:
            embed = discord.Embed(
                title=title or None,
                description=content[:4000],
                color=COLOR_MAP.get(color_key, discord.Color.blurple()),
            )
            embed.set_footer(text=f"Message envoyé par {user['username']} via le dashboard")
            await channel.send(content=prefix if mention_everyone else None, embed=embed)
    except discord.Forbidden:
        raise HTTPException(status_code=403, detail="Le bot n'a pas la permission d'écrire dans ce salon.")

    return {"status": "ok", "channel_id": channel_id}
