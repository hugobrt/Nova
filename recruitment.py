"""
COG RECRUTEMENT
================
- /config_recrutement [admin] : configure les salons + rôles du service RH
- /publier_recrutement [admin] : poste un message avec un bouton "Postuler spontanément"
- Postuler -> modal (poste souhaité, motivation, disponibilités) -> candidature enregistrée
  + notif dans le salon RH avec boutons Accepter/Refuser
- Accepter -> attribue le rôle employé (si configuré) + DM au candidat + entrée employés créée
- Refuser -> DM au candidat
- /sanctionner_employe [RH] : sanctionne un employé (log, pas d'action Discord directe)
- /virer_employe [RH] : licencie un employé, retire le rôle employé, DM
- /employes [RH] : liste les employés actifs

L'accès RH (accepter/refuser une candidature, sanctionner, virer) est ouvert
aux Administrateurs ET aux membres ayant le rôle configuré via hr_role_id.
Tout ceci est aussi pilotable depuis le dashboard web (mêmes règles de rôle).
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config_store import get_guild_config, set_guild_config
from recruitment_store import (
    create_recruitment_application, get_recruitment_application,
    set_recruitment_status, hire_employee, get_employee,
    list_employees, sanction_employee, fire_employee,
)

logger = logging.getLogger("Recruitment")


def is_hr(interaction: discord.Interaction, config: dict) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    hr_role_id = config.get("hr_role_id")
    if hr_role_id:
        return any(r.id == hr_role_id for r in interaction.user.roles)
    return False


# ---------------------------------------------------------------------
# Modal de candidature libre
# ---------------------------------------------------------------------

class RecruitmentModal(discord.ui.Modal, title="Candidature spontanée"):
    poste = discord.ui.TextInput(
        label="Poste souhaité",
        style=discord.TextStyle.short,
        max_length=100,
    )
    motivation = discord.ui.TextInput(
        label="Pourquoi veux-tu nous rejoindre ?",
        style=discord.TextStyle.paragraph,
        max_length=800,
    )
    disponibilite = discord.ui.TextInput(
        label="Tes disponibilités",
        style=discord.TextStyle.short,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        app_id = await create_recruitment_application(
            interaction.guild.id, interaction.user.id,
            self.poste.value, self.motivation.value, self.disponibilite.value,
        )
        if app_id is None:
            await interaction.response.send_message(
                "⚠️ Tu as déjà une candidature en attente de traitement.", ephemeral=True
            )
            return

        config = await get_guild_config(interaction.guild.id)
        log_channel_id = config.get("recruitment_log_channel_id")
        if log_channel_id:
            channel = interaction.guild.get_channel(log_channel_id)
            if channel:
                embed = discord.Embed(
                    title="📋 Nouvelle candidature spontanée",
                    color=discord.Color.blurple(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="Candidat", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
                embed.add_field(name="Poste souhaité", value=self.poste.value, inline=False)
                embed.add_field(name="Motivation", value=self.motivation.value[:500], inline=False)
                embed.add_field(name="Disponibilités", value=self.disponibilite.value, inline=False)
                embed.set_footer(text=f"Candidature #{app_id}")

                view = discord.ui.View(timeout=None)
                view.add_item(RecruitmentAcceptButton(app_id))
                view.add_item(RecruitmentDenyButton(app_id))
                await channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            "✅ Ta candidature a bien été envoyée ! Le staff RH va l'examiner.", ephemeral=True
        )


class RecruitmentEntryButton(discord.ui.DynamicItem[discord.ui.Button], template=r"recrutement:apply"):
    def __init__(self):
        super().__init__(
            discord.ui.Button(
                label="Postuler spontanément",
                style=discord.ButtonStyle.primary,
                emoji="📨",
                custom_id="recrutement:apply",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RecruitmentModal())


# ---------------------------------------------------------------------
# Boutons Accepter / Refuser (persistants)
# ---------------------------------------------------------------------

class RecruitmentAcceptButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"recrutement:accept:(?P<app_id>[0-9]+)",
):
    def __init__(self, app_id: int):
        super().__init__(
            discord.ui.Button(
                label="✅ Accepter",
                style=discord.ButtonStyle.success,
                custom_id=f"recrutement:accept:{app_id}",
            )
        )
        self.app_id = app_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["app_id"]))

    async def callback(self, interaction: discord.Interaction):
        config = await get_guild_config(interaction.guild.id)
        if not is_hr(interaction, config):
            await interaction.response.send_message("Permission insuffisante (rôle RH requis).", ephemeral=True)
            return

        application = await get_recruitment_application(self.app_id)
        if application is None:
            await interaction.response.send_message("⚠️ Candidature introuvable.", ephemeral=True)
            return

        member = interaction.guild.get_member(application["user_id"])
        role_id = config.get("employee_role_id")

        if member and role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Candidature spontanée acceptée")
                except discord.Forbidden:
                    pass

        await hire_employee(interaction.guild.id, application["user_id"], role_id)
        await set_recruitment_status(self.app_id, "accepted", interaction.user.id)

        if member:
            try:
                await member.send(f"🎉 Ta candidature a été **acceptée** sur **{interaction.guild.name}** ! Bienvenue dans l'équipe.")
            except discord.Forbidden:
                pass

        await interaction.response.edit_message(
            content=f"✅ Candidature acceptée par {interaction.user.mention}", embed=None, view=None
        )


class RecruitmentDenyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"recrutement:deny:(?P<app_id>[0-9]+)",
):
    def __init__(self, app_id: int):
        super().__init__(
            discord.ui.Button(
                label="❌ Refuser",
                style=discord.ButtonStyle.danger,
                custom_id=f"recrutement:deny:{app_id}",
            )
        )
        self.app_id = app_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["app_id"]))

    async def callback(self, interaction: discord.Interaction):
        config = await get_guild_config(interaction.guild.id)
        if not is_hr(interaction, config):
            await interaction.response.send_message("Permission insuffisante (rôle RH requis).", ephemeral=True)
            return

        application = await get_recruitment_application(self.app_id)
        await set_recruitment_status(self.app_id, "refused", interaction.user.id)

        if application:
            member = interaction.guild.get_member(application["user_id"])
            if member:
                try:
                    await member.send(f"❌ Ta candidature sur **{interaction.guild.name}** n'a pas été retenue.")
                except discord.Forbidden:
                    pass

        await interaction.response.edit_message(
            content=f"❌ Candidature refusée par {interaction.user.mention}", embed=None, view=None
        )


# ---------------------------------------------------------------------
# Cog principal
# ---------------------------------------------------------------------

class Recruitment(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_dynamic_items(RecruitmentEntryButton, RecruitmentAcceptButton, RecruitmentDenyButton)

    @app_commands.command(name="config_recrutement", description="[Admin] Configurer le recrutement et les rôles RH")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.describe(
        salon_recrutement="Salon où sera posté le bouton de candidature spontanée",
        salon_log_rh="Salon où le staff RH voit les candidatures à traiter",
        role_employe="Rôle attribué automatiquement à l'embauche",
        role_rh="Rôle autorisé à accepter/refuser/sanctionner/virer (en plus des admins)",
    )
    async def config_recrutement(
        self, interaction: discord.Interaction,
        salon_recrutement: discord.TextChannel, salon_log_rh: discord.TextChannel,
        role_employe: discord.Role = None, role_rh: discord.Role = None,
    ):
        fields = {
            "recruitment_channel_id": salon_recrutement.id,
            "recruitment_log_channel_id": salon_log_rh.id,
        }
        if role_employe:
            fields["employee_role_id"] = role_employe.id
        if role_rh:
            fields["hr_role_id"] = role_rh.id
        await set_guild_config(interaction.guild.id, **fields)
        await interaction.response.send_message("✅ Configuration RH enregistrée.", ephemeral=True)

    @app_commands.command(name="publier_recrutement", description="[Admin] Publier le message de candidature spontanée")
    @app_commands.checks.has_permissions(administrator=True)
    async def publier_recrutement(self, interaction: discord.Interaction):
        config = await get_guild_config(interaction.guild.id)
        channel_id = config.get("recruitment_channel_id")
        channel = interaction.guild.get_channel(channel_id) if channel_id else interaction.channel

        embed = discord.Embed(
            title="🚌 Rejoignez l'équipe !",
            description="Tu veux nous rejoindre ? Clique ci-dessous pour envoyer ta candidature, même sans offre publiée.",
            color=discord.Color.blurple(),
        )
        view = discord.ui.View(timeout=None)
        view.add_item(RecruitmentEntryButton())
        await channel.send(embed=embed, view=view)
        await interaction.response.send_message(f"✅ Message de recrutement publié dans {channel.mention}.", ephemeral=True)

    @app_commands.command(name="sanctionner_employe", description="[RH] Sanctionner un employé")
    @app_commands.describe(membre="Employé concerné", raison="Raison de la sanction")
    async def sanctionner_employe(self, interaction: discord.Interaction, membre: discord.Member, raison: str):
        config = await get_guild_config(interaction.guild.id)
        if not is_hr(interaction, config):
            await interaction.response.send_message("Permission insuffisante (rôle RH requis).", ephemeral=True)
            return

        await sanction_employee(interaction.guild.id, membre.id, raison, interaction.user.id)
        try:
            await membre.send(f"⚠️ Tu as été sanctionné sur **{interaction.guild.name}** : {raison}")
        except discord.Forbidden:
            pass
        await interaction.response.send_message(f"✅ {membre.mention} sanctionné.", ephemeral=True)

    @app_commands.command(name="virer_employe", description="[RH] Licencier un employé")
    @app_commands.describe(membre="Employé concerné", raison="Raison du licenciement")
    async def virer_employe(self, interaction: discord.Interaction, membre: discord.Member, raison: str):
        config = await get_guild_config(interaction.guild.id)
        if not is_hr(interaction, config):
            await interaction.response.send_message("Permission insuffisante (rôle RH requis).", ephemeral=True)
            return

        role_id = config.get("employee_role_id")
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role and role in membre.roles:
                try:
                    await membre.remove_roles(role, reason=f"Licenciement : {raison}")
                except discord.Forbidden:
                    pass

        await fire_employee(interaction.guild.id, membre.id, raison, interaction.user.id)
        try:
            await membre.send(f"🚪 Tu as été licencié de **{interaction.guild.name}** : {raison}")
        except discord.Forbidden:
            pass
        await interaction.response.send_message(f"✅ {membre.mention} licencié.", ephemeral=True)

    @app_commands.command(name="employes", description="[RH] Lister les employés actifs")
    async def employes(self, interaction: discord.Interaction):
        config = await get_guild_config(interaction.guild.id)
        if not is_hr(interaction, config):
            await interaction.response.send_message("Permission insuffisante (rôle RH requis).", ephemeral=True)
            return

        employees = await list_employees(interaction.guild.id, status="active")
        if not employees:
            await interaction.response.send_message("Aucun employé actif.", ephemeral=True)
            return

        embed = discord.Embed(title="👥 Employés actifs", color=discord.Color.green())
        for emp in employees[:20]:
            embed.add_field(name=f"<@{emp['user_id']}>", value=f"Embauché le {emp['hired_at'].strftime('%d/%m/%Y')}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Recruitment(bot))
