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
from employees_store import hire_employee, list_employees, sanction_employee, fire_employee
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
