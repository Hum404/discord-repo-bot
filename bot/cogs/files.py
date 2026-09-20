"""文件相关指令：上传 / 下载（含溯源）/ 列表 / 搜索 / 详情 / 历史 / 删除。"""
from __future__ import annotations

import io
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import RepoBot, fmt_size

log = logging.getLogger("repo-bot")


class FilesCog(commands.Cog, name="文件"):
    def __init__(self, bot: RepoBot):
        self.bot = bot

    async def _resolve_file(self, guild_id: int, ref: str):
        """支持文件编号（#3 或 3）或完整文件 ID。"""
        s = ref.strip()
        digits = s[1:] if s.startswith("#") else s
        if digits.isdigit():
            row = await self.bot.db.get_file_by_seq(guild_id, int(digits))
            if row is not None:
                return row
        return await self.bot.db.get_file(s.lower())

    # ────────────────────────── 上传 ──────────────────────────

    @app_commands.command(name="upload", description="上传文件到仓库")
    @app_commands.describe(file="要上传的文件", description="文件描述（可选）")
    async def upload(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        description: str = "",
    ):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        limit = self.bot.config.max_file_size_mb * 1024 * 1024
        if file.size > limit:
            await interaction.followup.send(
                f"❌ 文件过大（{fmt_size(file.size)}），上限为 "
                f"{self.bot.config.max_file_size_mb} MB。",
                ephemeral=True,
            )
            return

        storage, error = await self.bot.resolve_storage_channel(interaction.guild)
        if storage is None:
            await interaction.followup.send(f"❌ 存储频道不可用：{error}", ephemeral=True)
            return

        try:
            data = await file.read()
        except discord.HTTPException:
            await interaction.followup.send("❌ 读取附件失败，请重试。", ephemeral=True)
            return

        uploader = interaction.user
        embed = discord.Embed(
            title="📦 文件入库",
            description=description or discord.utils.escape_markdown(file.filename),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="文件名", value=file.filename, inline=False)
        embed.add_field(name="大小", value=fmt_size(file.size), inline=True)
        embed.add_field(
            name="上传者", value=f"{uploader.mention} (`{uploader.id}`)", inline=True
        )
        embed.set_footer(text="文件 ID 见入库回执")

        try:
            storage_msg = await storage.send(
                embed=embed,
                file=discord.File(io.BytesIO(data), filename=file.filename),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Bot 没有权限向存储频道发送文件。", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ 上传到存储频道失败：{exc.text or exc}", ephemeral=True
            )
            return

        file_id, seq = await self.bot.db.add_file(
            origin_guild_id=interaction.guild.id,
            name=file.filename,
            size=file.size,
            content_type=file.content_type,
            description=description,
            uploader_id=uploader.id,
            uploader_name=str(uploader),
            storage_channel_id=storage.id,
            storage_message_id=storage_msg.id,
        )

        # 回写文件 ID 到存储消息的 embed，方便管理员对照
        embed.set_footer(text=f"编号 #{seq} · 文件 ID：{file_id}")
        try:
            await storage_msg.edit(embed=embed)
        except discord.HTTPException:
            pass

        log_embed = discord.Embed(
            title="📤 上传记录",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.add_field(name="文件", value=f"`{file_id}` · {file.filename}", inline=False)
        log_embed.add_field(
            name="上传者", value=f"{uploader.mention} (`{uploader.id}`)", inline=True
        )
        log_embed.add_field(name="大小", value=fmt_size(file.size), inline=True)
        await self.bot.send_log(interaction.guild, log_embed)

        await interaction.followup.send(
            f"✅ 上传成功！\n"
            f"📄 文件名：`{file.filename}`\n"
            f"🔢 编号：`#{seq}`\n"
            f"🆔 文件 ID：`{file_id}`\n"
            f"💾 大小：{fmt_size(file.size)}\n"
            f"📥 下载方式：使用 `/download {seq}` 或 `/download {file_id}`",
            ephemeral=True,
        )

    # ────────────────────────── 下载（溯源核心） ──────────────────────────

    @app_commands.command(name="download", description="从仓库下载文件（下载行为会被记录）")
    @app_commands.describe(file_id="文件编号（如 3）或文件 ID（可用 /files 查询）")
    async def download(self, interaction: discord.Interaction, file_id: str):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        record = await self._resolve_file(interaction.guild.id, file_id)
        if record is None:
            await interaction.followup.send("❌ 找不到该文件，请检查编号或文件 ID。", ephemeral=True)
            return

        storage_channel = self.bot.get_channel(record["storage_channel_id"])
        if not isinstance(storage_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ 存储频道已不存在，无法取回文件。", ephemeral=True
            )
            return

        try:
            storage_msg = await storage_channel.fetch_message(record["storage_message_id"])
        except discord.NotFound:
            await interaction.followup.send(
                "❌ 存储消息已被删除，文件不可用。", ephemeral=True
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Bot 无权读取存储频道。", ephemeral=True
            )
            return

        if not storage_msg.attachments:
            await interaction.followup.send("❌ 存储消息中没有附件。", ephemeral=True)
            return

        attachment = storage_msg.attachments[0]
        try:
            data = await attachment.read()
        except discord.HTTPException:
            await interaction.followup.send("❌ 读取文件失败，请稍后重试。", ephemeral=True)
            return

        # ── 溯源：先落库，再发文件 ──
        user = interaction.user
        await self.bot.db.log_download(
            file_id=record["file_id"],
            user_id=user.id,
            user_name=str(user),
            guild_id=interaction.guild.id,
        )
        log.info(
            "下载记录：file=%s user=%s(%s) guild=%s",
            record["file_id"], user, user.id, interaction.guild.id,
        )

        log_embed = discord.Embed(
            title="📥 下载记录",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        log_embed.add_field(
            name="文件", value=f"`{record['file_id']}` · {record['name']}", inline=False
        )
        log_embed.add_field(
            name="下载者", value=f"{user.mention} (`{user.id}`)", inline=True
        )
        log_embed.add_field(
            name="累计下载", value=f"{record['download_count'] + 1} 次", inline=True
        )
        await self.bot.send_log(interaction.guild, log_embed)

        try:
            await interaction.followup.send(
                f"📦 `{record['name']}`（此下载已记录）",
                file=discord.File(io.BytesIO(data), filename=record["name"]),
                ephemeral=True,
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "❌ 发送文件失败（可能超出大小限制）。", ephemeral=True
            )

    # ────────────────────────── 浏览 / 搜索 ──────────────────────────

    @app_commands.command(name="files", description="查看仓库中最新的文件列表")
    @app_commands.describe(page="页码（默认第 1 页）")
    async def files(self, interaction: discord.Interaction, page: int = 1):
        assert interaction.guild is not None
        page = max(page, 1)
        per_page = 10
        total = await self.bot.db.count_files(interaction.guild.id)
        rows = await self.bot.db.list_files(
            interaction.guild.id, limit=per_page, offset=(page - 1) * per_page
        )
        if not rows:
            await interaction.response.send_message(
                "📭 仓库空空如也。" if page == 1 else "❌ 没有这一页。",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📚 文件仓库（第 {page} 页 / 共 {total} 个文件）",
            color=discord.Color.blurple(),
        )
        for row in rows:
            uploaded = datetime.fromtimestamp(row["uploaded_at"], tz=timezone.utc)
            no = f"#{row['seq']} · " if row["seq"] else ""
            embed.add_field(
                name=f"{no}`{row['file_id']}` · {row['name']}",
                value=(
                    f"大小：{fmt_size(row['size'])} · 下载：{row['download_count']} 次\n"
                    f"上传者：<@{row['uploader_id']}> · "
                    f"<t:{int(uploaded.timestamp())}:R>"
                    + (f"\n📝 {row['description']}" if row["description"] else "")
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="search", description="按文件名或描述搜索")
    @app_commands.describe(keyword="关键词")
    async def search(self, interaction: discord.Interaction, keyword: str):
        assert interaction.guild is not None
        rows = await self.bot.db.search_files(interaction.guild.id, keyword)
        if not rows:
            await interaction.response.send_message(
                f"🔍 没有找到与「{keyword}」相关的文件。", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"🔍 搜索「{keyword}」", color=discord.Color.blurple()
        )
        for row in rows:
            no = f"#{row['seq']} · " if row["seq"] else ""
            embed.add_field(
                name=f"{no}`{row['file_id']}` · {row['name']}",
                value=(
                    f"大小：{fmt_size(row['size'])} · 下载：{row['download_count']} 次 · "
                    f"上传者：<@{row['uploader_id']}>"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ────────────────────────── 详情 / 溯源 ──────────────────────────

    @app_commands.command(name="fileinfo", description="查看文件详细信息")
    @app_commands.describe(file_id="文件编号或文件 ID")
    async def fileinfo(self, interaction: discord.Interaction, file_id: str):
        assert interaction.guild is not None
        record = await self._resolve_file(interaction.guild.id, file_id)
        if record is None:
            await interaction.response.send_message("❌ 找不到该文件。", ephemeral=True)
            return

        uploaded = datetime.fromtimestamp(record["uploaded_at"], tz=timezone.utc)
        embed = discord.Embed(
            title=f"📄 {record['name']}", color=discord.Color.blurple()
        )
        if record["seq"]:
            embed.add_field(name="编号", value=f"`#{record['seq']}`", inline=True)
        embed.add_field(name="文件 ID", value=f"`{record['file_id']}`", inline=True)
        embed.add_field(name="大小", value=fmt_size(record["size"]), inline=True)
        embed.add_field(name="类型", value=record["content_type"] or "未知", inline=True)
        embed.add_field(
            name="上传者",
            value=f"<@{record['uploader_id']}> (`{record['uploader_id']}`)",
            inline=True,
        )
        embed.add_field(name="上传时间", value=f"<t:{int(uploaded.timestamp())}:F>", inline=True)
        embed.add_field(name="下载次数", value=f"{record['download_count']} 次", inline=True)
        if record["description"]:
            embed.add_field(name="描述", value=record["description"], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="history", description="查看文件的下载溯源记录（上传者或管理员可用）")
    @app_commands.describe(file_id="文件编号或文件 ID")
    async def history(self, interaction: discord.Interaction, file_id: str):
        assert interaction.guild is not None and isinstance(
            interaction.user, discord.Member
        )
        record = await self._resolve_file(interaction.guild.id, file_id)
        if record is None:
            await interaction.response.send_message("❌ 找不到该文件。", ephemeral=True)
            return

        is_uploader = record["uploader_id"] == interaction.user.id
        is_admin = interaction.user.guild_permissions.administrator
        if not (is_uploader or is_admin):
            await interaction.response.send_message(
                "❌ 只有文件上传者或管理员可以查看下载记录。", ephemeral=True
            )
            return

        rows = await self.bot.db.get_download_history(record["file_id"])
        no = f"#{record['seq']} · " if record["seq"] else ""
        embed = discord.Embed(
            title=f"🕵️ 下载溯源：{no}{record['name']}",
            color=discord.Color.gold(),
        )
        embed.set_footer(text=f"累计下载 {record['download_count']} 次，最多显示最近 20 条")
        if not rows:
            embed.description = "暂无下载记录。"
        else:
            lines = []
            for row in rows:
                lines.append(
                    f"• <@{row['user_id']}> (`{row['user_id']}`) — "
                    f"<t:{row['downloaded_at']}:F>"
                )
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ────────────────────────── 删除 ──────────────────────────

    @app_commands.command(name="delete", description="删除仓库中的文件（上传者或管理员可用）")
    @app_commands.describe(file_id="文件编号或文件 ID")
    async def delete(self, interaction: discord.Interaction, file_id: str):
        assert interaction.guild is not None and isinstance(
            interaction.user, discord.Member
        )
        record = await self._resolve_file(interaction.guild.id, file_id)
        if record is None:
            await interaction.response.send_message("❌ 找不到该文件。", ephemeral=True)
            return

        is_uploader = record["uploader_id"] == interaction.user.id
        is_admin = interaction.user.guild_permissions.administrator
        if not (is_uploader or is_admin):
            await interaction.response.send_message(
                "❌ 只有文件上传者或管理员可以删除该文件。", ephemeral=True
            )
            return

        # 尽量删除存储消息，失败也不阻塞
        storage_channel = self.bot.get_channel(record["storage_channel_id"])
        if isinstance(storage_channel, discord.TextChannel):
            try:
                msg = await storage_channel.fetch_message(record["storage_message_id"])
                await msg.delete()
            except discord.HTTPException:
                pass

        await self.bot.db.delete_file(record["file_id"])
        await interaction.response.send_message(
            f"🗑️ 文件 `{record['file_id']}` · {record['name']} 已删除。",
            ephemeral=True,
        )


async def setup(bot: RepoBot) -> None:
    await bot.add_cog(FilesCog(bot))
