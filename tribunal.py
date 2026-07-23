"""
COG TRIBUNAL
=============
Système de sanctions RH avec salon privé de contestation ("tribunal"),
et gestion des peines de Prison (retrait temporaire de tous les rôles,
restauration automatique à la fin de la peine).

Flux :
1. Un RH sanctionne un employé (dashboard) -> le bot donne le rôle
   "Sanctionné" (accès à la catégorie tribunal configurée) et crée un
   salon privé dédié avec le motif + un bouton "Contester".
2. Le membre peut contester : un modal envoie son explication, qui
   notifie le salon de log RH avec des boutons Annuler/Maintenir.
3. Si un staff (admin) juge la faute grave, il peut envoyer le membre
   en Prison depuis le dashboard avec une durée. Tous ses rôles sont
   sauvegardés puis retirés, le rôle Prison est donné. Une tâche de
   fond restaure automatiquement les rôles d'origine à la fin de la peine.
"""

import logging

import discord
from discord.ext import commands, tasks

from config_store import get_guild_config
from sanctions_store import (
    get_sanction, set_contest_message, get_sanction_by_channel,
    list_expired_prison_sentences, release_prison_sentence,
)

logger = logging.getLogger("Tribunal")


class ContestModal(discord.ui.Modal, title="Contester la sanction"):
    explanation = discord.ui.TextInput(
        label="Ton explication", style=discord.TextStyle.paragraph, max_length=1000
    )

    def __init__(self, sanction_id: int):
        super().__init__()
        self.sanction_id = sanction_id

    async def on_submit(self, interaction: discord.Interaction):
        await set_contest_message(self.sanction_id, self.explanation.value)

        config = await get_guild_config(interaction.guild.id)
        log_channel_id = config.get("hr_log_channel_id")
        if log_channel_id:
            channel = interaction.guild.get_channel(log_channel_id)
            if channel:
                embed = discord.Embed(
                    title=f"⚖️ Contestation — Sanction #{self.sanction_id}",
                    description=self.explanation.value,
                    color=discord.Color.gold(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="Contesté par", value=interaction.user.mention)
                view = discord.ui.View(timeout=None)
                view.add_item(CancelSanctionButton(self.sanction_id))
                view.add_item(UpholdSanctionButton(self.sanction_id))
                await channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            "✅ Ta contestation a été envoyée à la direction, en attente de décision.", ephemeral=True
        )


class ContestButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sanction:contest:(?P<sanction_id>[0-9]+)"):
    def __init__(self, sanction_id: int):
        super().__init__(
            discord.ui.Button(label="Contester", style=discord.ButtonStyle.primary, emoji="⚖️", custom_id=f"sanction:contest:{sanction_id}")
        )
        self.sanction_id = sanction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["sanction_id"]))

    async def callback(self, interaction: discord.Interaction):
        sanction = await get_sanction(self.sanction_id)
        if sanction is None or sanction["user_id"] != interaction.user.id:
            await interaction.response.send_message("⚠️ Tu n'es pas concerné par cette sanction.", ephemeral=True)
            return
        if sanction["status"] != "active":
            await interaction.response.send_message("Cette sanction n'est plus contestable.", ephemeral=True)
            return
        await interaction.response.send_modal(ContestModal(self.sanction_id))


async def _restore_after_resolution(interaction: discord.Interaction, sanction: dict, cancelled: bool):
    guild = interaction.guild
    member = guild.get_member(sanction["user_id"])
    config = await get_guild_config(guild.id)
    role_id = config.get("sanctioned_role_id")

    if cancelled:
        from employees_store import get_employee, hire_employee
        emp = await get_employee(guild.id, sanction["user_id"])
        if emp:
            await hire_employee(guild.id, sanction["user_id"], emp.get("role_id"))
        if member and role_id:
            role = guild.get_role(role_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Sanction annulée")
                except discord.Forbidden:
                    pass
        if member:
            try:
                await member.send(f"✅ Ta sanction sur **{guild.name}** a été **annulée**.")
            except discord.Forbidden:
                pass
    else:
        if member:
            try:
                await member.send(f"⚠️ Ta sanction sur **{guild.name}** a été **maintenue** après examen.")
            except discord.Forbidden:
                pass


class CancelSanctionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sanction:cancel:(?P<sanction_id>[0-9]+)"):
    def __init__(self, sanction_id: int):
        super().__init__(
            discord.ui.Button(label="Annuler la sanction", style=discord.ButtonStyle.success, emoji="✅", custom_id=f"sanction:cancel:{sanction_id}")
        )
        self.sanction_id = sanction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["sanction_id"]))

    async def callback(self, interaction: discord.Interaction):
        from sanctions_store import cancel_sanction
        sanction = await get_sanction(self.sanction_id)
        if sanction is None:
            await interaction.response.send_message("Sanction introuvable.", ephemeral=True)
            return
        await cancel_sanction(self.sanction_id, interaction.user.id)
        await _restore_after_resolution(interaction, sanction, cancelled=True)
        await interaction.response.edit_message(content="✅ Sanction annulée.", view=None, embed=interaction.message.embeds[0] if interaction.message.embeds else None)


class UpholdSanctionButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sanction:uphold:(?P<sanction_id>[0-9]+)"):
    def __init__(self, sanction_id: int):
        super().__init__(
            discord.ui.Button(label="Maintenir la sanction", style=discord.ButtonStyle.danger, emoji="🔒", custom_id=f"sanction:uphold:{sanction_id}")
        )
        self.sanction_id = sanction_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["sanction_id"]))

    async def callback(self, interaction: discord.Interaction):
        from sanctions_store import uphold_sanction
        sanction = await get_sanction(self.sanction_id)
        if sanction is None:
            await interaction.response.send_message("Sanction introuvable.", ephemeral=True)
            return
        await uphold_sanction(self.sanction_id, interaction.user.id)
        await _restore_after_resolution(interaction, sanction, cancelled=False)
        await interaction.response.edit_message(content="🔒 Sanction maintenue.", view=None, embed=interaction.message.embeds[0] if interaction.message.embeds else None)


class Tribunal(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_dynamic_items(ContestButton, CancelSanctionButton, UpholdSanctionButton)
        self.check_prison_releases.start()

    def cog_unload(self):
        self.check_prison_releases.cancel()

    @tasks.loop(minutes=1)
    async def check_prison_releases(self):
        try:
            expired = await list_expired_prison_sentences()
        except Exception as e:
            logger.error(f"Erreur lecture peines de prison expirées : {e}")
            return

        for sentence in expired:
            guild = self.bot.get_guild(sentence["guild_id"])
            if guild is None:
                continue
            member = guild.get_member(sentence["user_id"])
            config = await get_guild_config(guild.id)
            prison_role_id = config.get("prison_role_id")

            if member:
                if prison_role_id:
                    prison_role = guild.get_role(prison_role_id)
                    if prison_role and prison_role in member.roles:
                        try:
                            await member.remove_roles(prison_role, reason="Fin de la peine de prison")
                        except discord.Forbidden:
                            pass

                saved_ids = [int(rid) for rid in sentence["saved_roles"].split(",") if rid]
                roles_to_restore = [guild.get_role(rid) for rid in saved_ids]
                roles_to_restore = [r for r in roles_to_restore if r is not None]
                if roles_to_restore:
                    try:
                        await member.add_roles(*roles_to_restore, reason="Fin de la peine de prison — restauration des rôles")
                    except discord.Forbidden:
                        pass

                try:
                    await member.send(f"🔓 Ta peine de prison sur **{guild.name}** est terminée, tes rôles ont été restaurés.")
                except discord.Forbidden:
                    pass

            await release_prison_sentence(sentence["id"])
            logger.info(f"Peine de prison #{sentence['id']} libérée (guild {sentence['guild_id']}, user {sentence['user_id']}).")

    @check_prison_releases.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Tribunal(bot))
