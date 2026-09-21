"""文件相关指令：上传 / 下载（含溯源）/ 列表 / 搜索 / 详情 / 历史 / 删除。"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import RepoBot, fmt_size
from ..tracing import inject_trace

log = logging.getLogger("repo-bot")


def build_storage_embed(
    name: str,
    size: int,
    uploader: discord.User | discord.Member,
    description: str = "",
    has_password: bool = False,
) -> discord.Embed:
    """存储频道中的文件入库卡片。"""
    embed = discord.Embed(
        title="📦 文件入库",
        description=description or discord.utils.escape_markdown(name),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="文件名", value=name, inline=False)
    embed.add_field(name="大小", value=fmt_size(size), inline=True)
    embed.add_field(
        name="上传者", value=f"{uploader.mention} (`{uploader.id}`)", inline=True
    )
    if has_password:
        embed.add_field(name="密码保护", value="🔒 下载需要密码", inline=True)
    embed.set_footer(text="文件 ID 见入库回执")
    return embed


class UploadPasswordModal(discord.ui.Modal, title="🔒 设置下载密码"):
    """上传准备阶段：设置下载密码。"""

    password = discord.ui.TextInput(
        label="下载密码",
        placeholder="成员下载该文件时需要输入的密码",
        min_length=1,
        max_length=64,
    )

    def __init__(self, view: "UploadPrepView"):
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._view.password = str(self.password.value)
        await interaction.response.edit_message(
            embed=self._view.make_embed(), view=self._view
        )


class RenameFileModal(discord.ui.Modal, title="✏️ 重命名文件"):
    """上传准备阶段：修改入库文件名。"""

    new_name = discord.ui.TextInput(label="新文件名", max_length=100)

    def __init__(self, view: "UploadPrepView"):
        super().__init__()
        self._view = view
        self.new_name.default = view.name

    async def on_submit(self, interaction: discord.Interaction) -> None:
        new = str(self.new_name.value).strip()
        if not new or "/" in new or "\\" in new:
            await interaction.response.send_message(
                "❌ 文件名无效：不能为空，也不能包含 `/` 或 `\\`。",
                ephemeral=True,
            )
            return
        # 新名字没带扩展名时，自动保留原扩展名
        old_ext = os.path.splitext(self._view.original_name)[1]
        if old_ext and not os.path.splitext(new)[1]:
            new += old_ext
        self._view.name = new
        await interaction.response.edit_message(
            embed=self._view.make_embed(), view=self._view
        )


class UploadPrepView(discord.ui.View):
    """上传准备页：设置密码 / 重命名 / 确认上传 / 取消。"""

    def __init__(
        self,
        cog: "FilesCog",
        guild: discord.Guild,
        uploader: discord.User | discord.Member,
        attachment: discord.Attachment,
        data: bytes,
        description: str,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.uploader = uploader
        self.attachment = attachment
        self.data = data
        self.description = description
        self.original_name = attachment.filename
        self.name = attachment.filename
        self.password: str | None = None
        self.trace_enabled = True
        self._finished = False

    def make_embed(self) -> discord.Embed:
        desc = f"📄 文件名：**{self.name}**\n💾 大小：{fmt_size(len(self.data))}\n"
        if self.name != self.original_name:
            desc += f"✏️ 原名：`{self.original_name}`\n"
        desc += f"🔒 下载密码：{'已设置 ✅' if self.password else '未设置'}\n"
        desc += (
            "🔖 下载溯源标记：开启 ✅（下载副本会嵌入下载者标记）\n"
            if self.trace_enabled
            else "🔖 下载溯源标记：关闭（下载副本不嵌入标记）\n"
        )
        if self.description:
            desc += f"📝 描述：{self.description}\n"
        desc += "\n可点击「设置密码」「重命名」「溯源开关」调整，确认无误后点击「确认上传」。"
        return discord.Embed(
            title="📤 上传准备", description=desc, color=discord.Color.blurple()
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.uploader.id:
            await interaction.response.send_message(
                "❌ 只有上传者本人可以操作。", ephemeral=True
            )
            return False
        return True

    def _finish(self) -> None:
        self._finished = True
        self.stop()

    @discord.ui.button(label="设置密码", emoji="🔒", style=discord.ButtonStyle.secondary)
    async def set_password(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(UploadPasswordModal(self))

    @discord.ui.button(label="重命名", emoji="✏️", style=discord.ButtonStyle.secondary)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RenameFileModal(self))

    @discord.ui.button(label="溯源开关", emoji="🔖", style=discord.ButtonStyle.secondary)
    async def toggle_trace(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.trace_enabled = not self.trace_enabled
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="确认上传", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._finished:
            return
        self._finish()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="⏳ 上传中…",
                description=f"正在上传 **{self.name}**，请稍候。",
                color=discord.Color.blurple(),
            ),
            view=None,
        )
        await self.cog._do_upload(interaction, self)

    @discord.ui.button(label="取消", emoji="✖️", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._finished:
            return
        self._finish()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="已取消",
                description="上传已取消，未写入任何内容。",
                color=discord.Color.light_grey(),
            ),
            view=None,
        )


class DownloadPasswordModal(discord.ui.Modal, title="🔒 输入下载密码"):
    """下载加密文件时的密码核验。"""

    password = discord.ui.TextInput(label="下载密码", max_length=64)

    def __init__(self, cog: "FilesCog", file_id: str):
        super().__init__()
        self.cog = cog
        self.file_id = file_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        record = await self.cog.bot.db.get_file(self.file_id)
        if record is None:
            await interaction.response.send_message("❌ 文件已被删除。", ephemeral=True)
            return
        if str(self.password.value) != (record["password"] or ""):
            await interaction.response.send_message(
                "❌ 密码错误，下载已取消。", ephemeral=True
            )
            await self.cog.bot.log_admin(
                interaction.guild,
                interaction.user,
                f"⚠️ 下载 `#{record['seq']} {record['name']}` 时密码错误",
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog._deliver_file(interaction, record)


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

    @app_commands.command(name="upload", description="上传文件到仓库（可设置密码、重命名）")
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

        try:
            data = await asyncio.wait_for(file.read(), timeout=120)
        except Exception:
            await interaction.followup.send(
                "❌ 读取附件失败（Discord CDN 连接异常或超时），请稍后重试。",
                ephemeral=True,
            )
            return

        view = UploadPrepView(
            self, interaction.guild, interaction.user, file, data, description
        )
        await interaction.followup.send(embed=view.make_embed(), view=view, ephemeral=True)

    async def _do_upload(self, interaction: discord.Interaction, prep: UploadPrepView) -> None:
        """确认上传：写入存储频道并登记入库。"""
        guild = prep.guild
        storage, error = await self.bot.resolve_storage_channel(guild)
        if storage is None:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="❌ 存储频道不可用",
                    description=error,
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return

        uploader = prep.uploader
        embed = build_storage_embed(
            prep.name, len(prep.data), uploader, prep.description, prep.password is not None
        )
        try:
            storage_msg = await storage.send(
                embed=embed,
                file=discord.File(io.BytesIO(prep.data), filename=prep.name),
            )
        except discord.Forbidden:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="❌ 上传失败",
                    description="Bot 没有权限向存储频道发送文件。",
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return
        except discord.HTTPException as exc:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="❌ 上传失败",
                    description=f"上传到存储频道失败：{exc.text or exc}",
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return
        except Exception:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="❌ 上传失败",
                    description="网络异常或超时，请稍后重试。",
                    color=discord.Color.red(),
                ),
                view=None,
            )
            return

        file_id, seq = await self.bot.db.add_file(
            origin_guild_id=guild.id,
            name=prep.name,
            size=len(prep.data),
            content_type=prep.attachment.content_type,
            description=prep.description,
            uploader_id=uploader.id,
            uploader_name=str(uploader),
            storage_channel_id=storage.id,
            storage_message_id=storage_msg.id,
            password=prep.password,
            trace_enabled=prep.trace_enabled,
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
        log_embed.add_field(name="文件", value=f"`{file_id}` · {prep.name}", inline=False)
        log_embed.add_field(
            name="上传者", value=f"{uploader.mention} (`{uploader.id}`)", inline=True
        )
        log_embed.add_field(name="大小", value=fmt_size(len(prep.data)), inline=True)
        if prep.password:
            log_embed.add_field(name="密码保护", value="🔒 是", inline=True)
        if not prep.trace_enabled:
            log_embed.add_field(name="溯源标记", value="已关闭 ⚠️", inline=True)
        await self.bot.send_log(guild, log_embed)

        # 管理日志：上传时重命名 / 关闭溯源，均同步记录
        if prep.name != prep.original_name:
            await self.bot.log_admin(
                guild,
                uploader,
                f"✏️ 上传时重命名文件：`{prep.original_name}` → `{prep.name}`（编号 `#{seq}`）",
            )
        if not prep.trace_enabled:
            await self.bot.log_admin(
                guild,
                uploader,
                f"⚠️ 上传时关闭了溯源标记：`{prep.name}`（编号 `#{seq}`）",
            )

        desc = (
            f"📄 文件名：`{prep.name}`\n"
            f"🔢 编号：`#{seq}`\n"
            f"🆔 文件 ID：`{file_id}`\n"
            f"💾 大小：{fmt_size(len(prep.data))}\n"
            f"🔒 下载密码：{'已设置' if prep.password else '无'}\n"
            f"🔖 溯源标记：{'开启' if prep.trace_enabled else '关闭'}\n"
            f"📥 下载方式：使用 `/download {seq}` 或 `/download {file_id}`"
        )
        if prep.name != prep.original_name:
            desc += f"\n✏️ 原名：`{prep.original_name}`"
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="✅ 上传成功", description=desc, color=discord.Color.green()
            ),
            view=None,
        )

    # ────────────────────────── 下载（溯源核心） ──────────────────────────

    @app_commands.command(name="download", description="从仓库下载文件（下载行为会被记录）")
    @app_commands.describe(file_id="文件编号（如 3）或文件 ID（可用 /files 查询）")
    async def download(self, interaction: discord.Interaction, file_id: str):
        assert interaction.guild is not None
        record = await self._resolve_file(interaction.guild.id, file_id)
        if record is None:
            await interaction.response.send_message(
                "❌ 找不到该文件，请检查编号或文件 ID。", ephemeral=True
            )
            return
        # 加密文件：先弹窗验证密码（Modal 必须是首个响应，不能先 defer）
        if record["password"]:
            await interaction.response.send_modal(
                DownloadPasswordModal(self, record["file_id"])
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self._deliver_file(interaction, record)

    async def _deliver_file(self, interaction: discord.Interaction, record) -> None:
        """从存储频道取回文件并发送，落库下载记录。调用前必须已 defer。"""
        assert interaction.guild is not None
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
        except Exception:
            await interaction.followup.send(
                "❌ 读取存储频道失败（网络异常），请稍后重试。", ephemeral=True
            )
            return

        if not storage_msg.attachments:
            await interaction.followup.send("❌ 存储消息中没有附件。", ephemeral=True)
            return

        attachment = storage_msg.attachments[0]
        try:
            data = await asyncio.wait_for(attachment.read(), timeout=180)
        except Exception:
            await interaction.followup.send(
                "❌ 读取文件失败（Discord CDN 连接异常或超时），请稍后重试。", ephemeral=True
            )
            return

        # ── 溯源：向本次下载的副本注入下载者标记（存储原文件不受影响） ──
        # 图片水印是 CPU 密集操作，放到线程里执行，避免阻塞事件循环
        user = interaction.user
        if record["trace_enabled"]:
            data, traced = await asyncio.to_thread(
                inject_trace,
                data, record["name"], user.id, str(user), record["content_type"],
            )
        else:
            traced = False

        # ── 溯源：先落库，再发文件 ──
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
        if traced:
            log_embed.add_field(name="溯源标记", value="🔖 已注入", inline=True)
        await self.bot.send_log(interaction.guild, log_embed)

        try:
            note = "此下载已记录，文件内嵌溯源标记" if traced else "此下载已记录"
            await interaction.followup.send(
                f"📦 `{record['name']}`（{note}）",
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
        embed.add_field(
            name="密码保护",
            value="🔒 下载需要密码" if record["password"] else "无",
            inline=True,
        )
        embed.add_field(
            name="溯源标记",
            value="🔖 开启" if record["trace_enabled"] else "关闭",
            inline=True,
        )
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
        if is_admin and not is_uploader:
            await self.bot.log_admin(
                interaction.guild,
                interaction.user,
                f"🗑️ 删除文件 `#{record['seq']} {record['name']}`"
                f"（上传者 <@{record['uploader_id']}>）",
            )
        await interaction.response.send_message(
            f"🗑️ 文件 `{record['file_id']}` · {record['name']} 已删除。",
            ephemeral=True,
        )


async def setup(bot: RepoBot) -> None:
    await bot.add_cog(FilesCog(bot))
