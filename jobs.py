"""
COG OFFRES D'EMPLOI
====================
Le bot garde uniquement la logique nécessaire pour que le message Discord
(bouton "Postuler") continue de fonctionner après un redémarrage.
La création/gestion des offres et des candidatures se pilote depuis le
dashboard (app.py) — plus besoin de commandes slash pour ça.
"""

import re
import logging

import discord
from discord.ext import commands

from jobs_store import get_job, add_application, set_application_status

logger = logging.getLogger("Jobs")


def job_embed(job: dict) -> discord.Embed:
    color = discord.Color.green() if job["status"] == "open" else discord.Color.greyple()
    embed = discord.Embed(
        title=f"🚌 {job['title']}",
        description=job["description"],
        color=color,
    )
    embed.add_field(name="Statut", value="🟢 Ouvert" if job["status"] == "open" else "🔴 Clôturé", inline=True)
    embed.add_field(name="Candidatures", value=str(len(job["applications"])), inline=True)
    embed.set_footer(text=f"Offre #{job['id']}")
    return embed


# ---------------------------------------------------------------------
# Modal de candidature (déclenché par le bouton Postuler)
# ---------------------------------------------------------------------

class ApplicationModal(discord.ui.Modal, title="Candidater"):
    motivation = discord.ui.TextInput(
        label="Pourquoi veux-tu ce poste ?",
        style=discord.TextStyle.paragraph,
        max_length=800,
    )
    disponibilite = discord.ui.TextInput(
        label="Tes disponibilités",
        style=discord.TextStyle.short,
        max_length=200,
    )

    def __init__(self, job_id: int):
        super().__init__()
        self.job_id = job_id

    async def on_submit(self, interaction: discord.Interaction):
        job = await get_job(self.job_id)
        if job is None or job["status"] != "open":
            await interaction.response.send_message("⚠️ Cette offre n'est plus disponible.", ephemeral=True)
            return

        added = await add_application(self.job_id, interaction.user.id, self.motivation.value, self.disponibilite.value)
        if not added:
            await interaction.response.send_message("Tu as déjà postulé à cette offre.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ Candidature envoyée pour **{job['title']}** ! Le staff va l'examiner sur le dashboard.", ephemeral=True
        )


# ---------------------------------------------------------------------
# Bouton dynamique (survit aux redémarrages, job_id encodé dans le custom_id)
# ---------------------------------------------------------------------

class JobApplyButton(discord.ui.DynamicItem[discord.ui.Button], template=r"job:apply:(?P<job_id>[0-9]+)"):
    def __init__(self, job_id: int):
        super().__init__(
            discord.ui.Button(
                label="Postuler",
                style=discord.ButtonStyle.primary,
                emoji="📨",
                custom_id=f"job:apply:{job_id}",
            )
        )
        self.job_id = job_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(int(match["job_id"]))

    async def callback(self, interaction: discord.Interaction):
        job = await get_job(self.job_id)
        if job is None or job["status"] != "open":
            await interaction.response.send_message("⚠️ Cette offre n'est plus disponible.", ephemeral=True)
            return
        await interaction.response.send_modal(ApplicationModal(self.job_id))


class Jobs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_dynamic_items(JobApplyButton)


async def setup(bot: commands.Bot):
    await bot.add_cog(Jobs(bot))
