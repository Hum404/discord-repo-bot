"""管理员指令：存储模式设置、日志频道、审计查询。"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import RepoBot, fmt_size

admin_only = app_commands.checks.has_permissions(administrator=True)


class AdminCog(commands.Cog, name="管理"):
    def __init__(self, bot: RepoBot):
        self.bot = bot

    # ───────────────────── 存储模式 ─────────────────────

    @app_commands.command(name="storage_mode", description="设置文件存储方式（管理员）")
    @app_commands.describe(
        mode="category=在当前服务器创建专用子区存储；guild=存储到独立服务器",
        storage_guild_id="选择 guild 模式时填写存储服务器 ID（默认读取 .env 配置）",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="当前服务器子区（category）", value="category"),
            app_commands.Choice(name="独立存储服务器（guild）", value="guild"),
        ]
    )
    @admin_only
    async def storage_mode(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
        storage_guild_id: str | None = None,
    ):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        fields: dict = {"storage_mode": mode.value, "storage_channel_id": None}
        if mode.value == "guild":
            guild_id: int | None = None
            if storage_guild_id and storage_guild_id.strip().isdigit():
                guild_id = int(storage_guild_id.strip())
            elif self.bot.config.storage_guild_id:
                guild_id = self.bot.config.storage_guild_id
            if not guild_id:
                await interaction.followup.send(
                    "❌ 未指定存储服务器。请在 .env 中设置 STORAGE_GUILD_ID，"
                    "并把 Bot 邀请进该服务器。",
                    ephemeral=True,
                )
                return
            if self.bot.get_guild(guild_id) is None:
                await interaction.followup.send(
                    f"❌ Bot 不在服务器 (ID: {guild_id}) 中，请先邀请 Bot 加入。",
                    ephemeral=True,
                )
                return
            fields["storage_guild_id"] = guild_id

        await self.bot.db.upsert_settings(interaction.guild.id, **fields)

        storage, error = await self.bot.resolve_storage_channel(interaction.guild)
        if storage is None:
            await interaction.followup.send(
                f"⚠️ 已保存设置，但初始化存储频道失败：{error}", ephemeral=True
            )
            return

        mode_text = "当前服务器子区" if mode.value == "category" else "独立存储服务器"
        await interaction.followup.send(
            f"✅ 存储模式已设置为 **{mode_text}**\n"
            f"📁 存储位置：{storage.guild.name} → #{storage.name}",
            ephemeral=True,
        )

    # ───────────────────── 日志频道 ─────────────────────

    @app_commands.command(name="log_channel", description="设置上传/下载审计日志频道（管理员）")
    @app_commands.describe(channel="日志发送到的频道；不填则关闭日志")
    @admin_only
    async def log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ):
        assert interaction.guild is not None
        await self.bot.db.upsert_settings(
            interaction.guild.id, log_channel_id=channel.id if channel else None
        )
        if channel:
            await interaction.response.send_message(
                f"✅ 审计日志将发送到 {channel.mention}。", ephemeral=True
            )
        else:
            await interaction.response.send_message("✅ 已关闭审计日志。", ephemeral=True)

    # ───────────────────── 用户下载审计 ─────────────────────

    @app_commands.command(name="audit_user", description="查看某成员的下载记录（管理员）")
    @app_commands.describe(member="要审计的成员")
    @admin_only
    async def audit_user(
        self, interaction: discord.Interaction, member: discord.Member
    ):
        assert interaction.guild is not None
        rows = await self.bot.db.get_user_downloads(interaction.guild.id, member.id)
        embed = discord.Embed(
            title=f"🕵️ 成员下载审计：{member} (`{member.id}`)",
            color=discord.Color.gold(),
        )
        if not rows:
            embed.description = "该成员暂无下载记录。"
        else:
            lines = [
                f"• `{row['file_id']}` · {row['file_name']} — <t:{row['downloaded_at']}:F>"
                for row in rows
            ]
            embed.description = "\n".join(lines)
            embed.set_footer(text=f"最多显示最近 {len(rows)} 条")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ───────────────────── 仓库统计 ─────────────────────

    @app_commands.command(name="stats", description="查看仓库统计信息（管理员）")
    @admin_only
    async def stats(self, interaction: discord.Interaction):
        assert interaction.guild is not None
        total = await self.bot.db.count_files(interaction.guild.id)
        cur = await self.bot.db.conn.execute(
            """
            SELECT COALESCE(SUM(size), 0), COALESCE(SUM(download_count), 0)
            FROM files WHERE origin_guild_id = ?
            """,
            (interaction.guild.id,),
        )
        size_sum, dl_sum = await cur.fetchone()
        settings = await self.bot.db.get_settings(interaction.guild.id)
        mode = (settings["storage_mode"] if settings else "category")
        mode_text = "当前服务器子区" if mode == "category" else "独立存储服务器"

        embed = discord.Embed(title="📊 仓库统计", color=discord.Color.blurple())
        embed.add_field(name="文件总数", value=str(total), inline=True)
        embed.add_field(name="总大小", value=fmt_size(size_sum), inline=True)
        embed.add_field(name="累计下载", value=f"{dl_sum} 次", inline=True)
        embed.add_field(name="存储模式", value=mode_text, inline=True)
        if settings and settings["log_channel_id"]:
            embed.add_field(
                name="日志频道", value=f"<#{settings['log_channel_id']}>", inline=True
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: RepoBot) -> None:
    await bot.add_cog(AdminCog(bot))
