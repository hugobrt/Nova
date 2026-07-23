"""
COG PORTAIL EMPLOYÉ
=====================
Publie un message avec 3 boutons pour les employés :
- 🗓️ Demande de congé/absence
- 🚨 Rapport d'incident (perturbation, excès de vitesse, etc.)
- 🎫 Ouvrir un ticket (salon privé avec la direction)

Toute la validation/gestion se fait depuis le dashboard (app.py).
Le bot ne fait que collecter les demandes et créer les salons de ticket.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config_store import get_guild_config, set_guild_config
from requests_store import (
    create_leave_request, create_incident_report,
    create_ticket, attach_ticket_channel, get_ticket_by_channel, close_ticket,
)

logger = logging.getLogger("Portal")

INCIDENT_TYPES = [
    "Perturbation sur une ligne",
    "Excès de vitesse / infraction conducteur",
    "Accident / incident matériel",
    "Retard important",
    "Autre",
]


# ---------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------

class LeaveRequestModal(discord.ui.Modal, title="Demande de congé / absence"):
    start_date = discord.ui.TextInput(label="Date de début (jj/mm/aaaa)", max_length=10)
    end_date = discord.ui.TextInput(label="Date de fin (jj/mm/aaaa)", max_length=10)
    reason = discord.ui.TextInput(label="Motif", style=discord.TextStyle.paragraph, max_length=500, required=False)

    async def on_submit(self, interaction: discord.Interaction):
        leave_id = await create_leave_request(
            interaction.guild.id, interaction.user.id,
            self.start_date.value, self.end_date.value, self.reason.value or "",
        )
        config = await get_guild_config(interaction.guild.id)
        log_channel_id = config.get("hr_log_channel_id")
        if log_channel_id:
            channel = interaction.guild.get_channel(log_channel_id)
            if channel:
                embed = discord.Embed(
                    title="🗓️ Nouvelle demande de congé",
                    color=discord.Color.blue(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="Employé", value=f"{interaction.user.mention}", inline=False)
                embed.add_field(name="Période", value=f"{self.start_date.value} → {self.end_date.value}", inline=False)
                if self.reason.value:
                    embed.add_field(name="Motif", value=self.reason.value, inline=False)
                embed.set_footer(text=f"Demande #{leave_id} — à traiter sur le dashboard")
                await channel.send(embed=embed)

        await interaction.response.send_message(
            "✅ Ta demande de congé a été envoyée, la direction va l'examiner sur le dashboard.", ephemeral=True
        )


class IncidentReportModal(discord.ui.Modal, title="Rapport d'incident"):
    line_info = discord.ui.TextInput(label="Ligne / véhicule concerné", max_length=100, required=False)
    description = discord.ui.TextInput(label="Description de l'incident", style=discord.TextStyle.paragraph, max_length=1000)

    def __init__(self, incident_type: str):
        super().__init__()
        self.incident_type = incident_type

    async def on_submit(self, interaction: discord.Interaction):
        incident_id = await create_incident_report(
            interaction.guild.id, interaction.user.id,
            self.incident_type, self.line_info.value or "", self.description.value,
        )
        config = await get_guild_config(interaction.guild.id)
        log_channel_id = config.get("hr_log_channel_id")
        if log_channel_id:
            channel = interaction.guild.get_channel(log_channel_id)
            if channel:
                embed = discord.Embed(
                    title="🚨 Nouveau rapport d'incident",
                    color=discord.Color.orange(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="Rapporté par", value=f"{interaction.user.mention}", inline=False)
                embed.add_field(name="Type", value=self.incident_type, inline=True)
                if self.line_info.value:
                    embed.add_field(name="Ligne / véhicule", value=self.line_info.value, inline=True)
                embed.add_field(name="Description", value=self.description.value, inline=False)
                embed.set_footer(text=f"Incident #{incident_id} — à traiter sur le dashboard")
                await channel.send(embed=embed)

        await interaction.response.send_message(
            "✅ Ton rapport d'incident a été envoyé à la direction.", ephemeral=True
        )


class IncidentTypeSelect(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)

    @discord.ui.select(
        placeholder="Choisis le type d'incident",
        options=[discord.SelectOption(label=t) for t in INCIDENT_TYPES],
    )
    async def select_type(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.send_modal(IncidentReportModal(select.values[0]))


class TicketSubjectModal(discord.ui.Modal, title="Ouvrir un ticket"):
    subject = discord.ui.TextInput(label="Sujet de ta demande", max_length=150)

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        config = await get_guild_config(guild.id)
        category_id = config.get("tickets_category_id")
        category = guild.get_channel(category_id) if category_id else None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        hr_role_id = config.get("hr_role_id")
        if hr_role_id:
            hr_role = guild.get_role(hr_role_id)
            if hr_role:
                overwrites[hr_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        ticket_id = await create_ticket(guild.id, interaction.user.id, self.subject.value)
        channel_name = f"ticket-{ticket_id}-{interaction.user.name}"[:95]

        try:
            channel = await guild.create_text_channel(
                channel_name, category=category, overwrites=overwrites,
                topic=f"Ticket #{ticket_id} — {self.subject.value}",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ Le bot n'a pas la permission de créer un salon. Contacte un admin.", ephemeral=True
            )
            return

        await attach_ticket_channel(ticket_id, channel.id)

        embed = discord.Embed(
            title=f"🎫 Ticket #{ticket_id}",
            description=self.subject.value,
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"Ouvert par {interaction.user}")
        view = discord.ui.View(timeout=None)
        view.add_item(CloseTicketButton(ticket_id))
        await channel.send(content=f"{interaction.user.mention}", embed=embed, view=view)

        await interaction.response.send_message(f"✅ Ton ticket a été créé : {channel.mention}", ephemeral=True)


class CloseTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"ticket:close:(?P<ticket_id>[0-9]+)"):
    def __init__(self, ticket_id: int):
        super().__init__(
            discord.ui.Button(label="Fermer le ticket", style=discord.ButtonStyle.danger, emoji="🔒", custom_id=f"ticket:close:{ticket_id}")
        )
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["ticket_id"]))

    async def callback(self, interaction: discord.Interaction):
        await close_ticket(self.ticket_id, interaction.user.id)
        await interaction.response.send_message("🔒 Ticket fermé, ce salon sera archivé.", ephemeral=False)
        try:
            await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:95])
        except discord.Forbidden:
            pass


# ---------------------------------------------------------------------
# Menu principal (3 boutons)
# ---------------------------------------------------------------------

class PortalLeaveButton(discord.ui.DynamicItem[discord.ui.Button], template=r"portal:leave"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Demande de congé", style=discord.ButtonStyle.primary, emoji="🗓️", custom_id="portal:leave"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LeaveRequestModal())


class PortalIncidentButton(discord.ui.DynamicItem[discord.ui.Button], template=r"portal:incident"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Rapport d'incident", style=discord.ButtonStyle.secondary, emoji="🚨", custom_id="portal:incident"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Sélectionne le type d'incident :", view=IncidentTypeSelect(), ephemeral=True)


class PortalTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"portal:ticket"):
    def __init__(self):
        super().__init__(discord.ui.Button(label="Ouvrir un ticket", style=discord.ButtonStyle.success, emoji="🎫", custom_id="portal:ticket"))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketSubjectModal())


class Portal(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_dynamic_items(
            PortalLeaveButton, PortalIncidentButton, PortalTicketButton, CloseTicketButton,
        )

    @app_commands.command(name="publier_portail", description="[Admin] Publier le portail employé (congés, incidents, tickets)")
    @app_commands.checks.has_permissions(administrator=True)
    async def publier_portail(self, interaction: discord.Interaction):
        config = await get_guild_config(interaction.guild.id)
        channel_id = config.get("employee_portal_channel_id")
        channel = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel

        embed = discord.Embed(
            title="👔 Portail employé",
            description=(
                "Utilise les boutons ci-dessous pour :\n"
                "🗓️ Faire une demande de congé/absence\n"
                "🚨 Signaler un incident (perturbation, excès de vitesse, accident...)\n"
                "🎫 Ouvrir un ticket privé avec la direction"
            ),
            color=discord.Color.blurple(),
        )
        view = discord.ui.View(timeout=None)
        view.add_item(PortalLeaveButton())
        view.add_item(PortalIncidentButton())
        view.add_item(PortalTicketButton())
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ Portail employé publié dans {channel.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Portal(bot))
