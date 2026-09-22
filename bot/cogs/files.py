"""文件相关指令：上传 / 下载（含溯源）/ 列表 / 搜索 / 详情 / 历史 / 删除。"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import RepoBot, fmt_size
from ..tracing import inject_trace

log = logging.getLogger("repo-bot")


def fmt_duration(seconds: float) -> str:
    """把秒数格式化为「X 小时 X 分钟」。"""
    s = max(0, int(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h} 小时 {m} 分钟"
    if m:
        return f"{m} 分钟"
    return f"{s} 秒"


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


APPEAL_VOTES_REQUIRED = 2  # 解封/驳回各需的管理员票数


class AppealVoteView(discord.ui.View):
    """解封申诉工单：管理员投票，集齐 2 票同意即解封，2 票拒绝即驳回。

    按钮使用固定 custom_id，配合 cog_load 中的 add_view 恢复，
    Bot 重启后历史工单仍可继续投票（票数清零重新计）。
    """

    def __init__(
        self,
        bot: RepoBot,
        guild_id: int,
        user_id: int,
        ban_reason: str,
        banned_until: float,
        appeal_reason: str,
        appeal_id: int | None = None,
    ):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.ban_reason = ban_reason
        self.banned_until = banned_until
        self.appeal_reason = appeal_reason
        self.appeal_id = appeal_id
        self.yes_voters: set[int] = set()
        self.no_voters: set[int] = set()
        self.message: discord.Message | None = None

    def initial_embed(self, member: discord.Member | discord.User) -> discord.Embed:
        embed = discord.Embed(
            title="🎫 解封申诉工单",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="申请人", value=f"{member.mention} (`{member.id}`)", inline=False
        )
        embed.add_field(name="封禁原因", value=self.ban_reason or "（未知）", inline=False)
        embed.add_field(
            name="原定解封", value=f"<t:{int(self.banned_until)}:R>", inline=True
        )
        embed.add_field(
            name="申诉理由", value=self.appeal_reason or "（未填写）", inline=False
        )
        embed.add_field(name="投票进度", value=self._progress_text(), inline=False)
        return embed

    def _progress_text(self) -> str:
        return (
            f"✅ 同意解封 {len(self.yes_voters)}/{APPEAL_VOTES_REQUIRED}　"
            f"❌ 拒绝 {len(self.no_voters)}/{APPEAL_VOTES_REQUIRED}"
        )

    def _update_embed(
        self, message: discord.Message, result: str | None = None
    ) -> discord.Embed:
        """在原工单嵌入上更新投票进度（重启后恢复的视图也能正确更新）。"""
        if message.embeds:
            embed = message.embeds[0]
        else:
            embed = discord.Embed(title="🎫 解封申诉工单", color=discord.Color.orange())
        for i, field in enumerate(embed.fields):
            if field.name == "投票进度":
                embed.set_field_at(
                    i, name="投票进度", value=self._progress_text(), inline=False
                )
                break
        else:
            embed.add_field(name="投票进度", value=self._progress_text(), inline=False)
        if result is not None:
            embed.add_field(name="处理结果", value=result, inline=False)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if isinstance(user, discord.Member) and user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "❌ 只有管理员可以处理工单。", ephemeral=True
        )
        return False

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _vote(self, interaction: discord.Interaction, approve: bool) -> None:
        uid = interaction.user.id
        if uid in self.yes_voters or uid in self.no_voters:
            await interaction.response.send_message("你已经投过票了。", ephemeral=True)
            return
        (self.yes_voters if approve else self.no_voters).add(uid)
        if len(self.yes_voters) >= APPEAL_VOTES_REQUIRED:
            await self._finish(interaction, approved=True)
        elif len(self.no_voters) >= APPEAL_VOTES_REQUIRED:
            await self._finish(interaction, approved=False)
        else:
            await interaction.response.edit_message(
                embed=self._update_embed(interaction.message), view=self
            )

    async def _finish(self, interaction: discord.Interaction, *, approved: bool) -> None:
        self._disable()
        result_text = (
            f"✅ 申诉通过，已解除限制（{interaction.user.mention} 等 "
            f"{APPEAL_VOTES_REQUIRED} 名管理员同意）"
            if approved
            else f"❌ 申诉驳回，封禁继续生效（{interaction.user.mention} 等 "
            f"{APPEAL_VOTES_REQUIRED} 名管理员拒绝）"
        )
        embed = self._update_embed(interaction.message, result=result_text)
        embed.color = discord.Color.green() if approved else discord.Color.red()
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

        if approved:
            await self.bot.db.unban_user(self.guild_id, self.user_id)
        if self.appeal_id is not None:
            await self.bot.db.close_appeal(
                self.appeal_id, "approved" if approved else "rejected"
            )
        else:  # 兼容路径：按用户关闭未结案工单
            await self.bot.db.conn.execute(
                "UPDATE appeals SET status = ? "
                "WHERE guild_id = ? AND user_id = ? AND status = 'open'",
                ("approved" if approved else "rejected", self.guild_id, self.user_id),
            )
            await self.bot.db.conn.commit()

        try:
            user = await self.bot.fetch_user(self.user_id)
            await user.send(
                "✅ 你的解封申诉已通过，限制已解除。"
                if approved
                else "❌ 你的解封申诉被驳回，封禁继续生效，到期自动解除。"
            )
        except discord.HTTPException:
            pass
        guild = self.bot.get_guild(self.guild_id)
        if guild is not None:
            await self.bot.log_admin(
                guild,
                interaction.user,
                f"🎫 申诉工单{'已通过（已解封）' if approved else '已驳回'}：<@{self.user_id}>",
            )

    @discord.ui.button(
        label="同意解封", emoji="✅",
        style=discord.ButtonStyle.success, custom_id="appeal:yes",
    )
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, approve=True)

    @discord.ui.button(
        label="拒绝", emoji="❌",
        style=discord.ButtonStyle.danger, custom_id="appeal:no",
    )
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, approve=False)


class FilesCog(commands.Cog, name="文件"):
    def __init__(self, bot: RepoBot):
        self.bot = bot

    async def cog_load(self) -> None:
        """恢复未结案申诉工单的投票按钮（重启后仍可投票）。"""
        try:
            rows = await self.bot.db.list_open_appeals()
        except Exception:
            log.exception("读取未结案申诉工单失败")
            return
        for row in rows:
            view = AppealVoteView(
                self.bot, row["guild_id"], row["user_id"],
                ban_reason="", banned_until=row["created_at"], appeal_reason="",
                appeal_id=row["id"],
            )
            self.bot.add_view(view, message_id=row["message_id"])
        if rows:
            log.info("已恢复 %d 个未结案申诉工单的投票按钮", len(rows))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """风控封禁拦截：被封禁成员无法使用本 cog 的任何指令（/appeal 除外）。"""
        ban = await self._check_ban(interaction)
        if ban is None:
            return True
        remaining = fmt_duration(ban["banned_until"] - time.time())
        try:
            await interaction.response.send_message(
                f"⛔ 你已被风控限制，**{remaining}**后解除。\n"
                f"原因：{ban['reason']}\n"
                "如有异议可使用 `/appeal` 提交申诉工单。",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return False

    async def _check_ban(self, interaction: discord.Interaction):
        """返回生效中的封禁记录；管理员与 /appeal 指令不受限。"""
        if interaction.guild is None:
            return None
        user = interaction.user
        if isinstance(user, discord.Member) and user.guild_permissions.administrator:
            return None
        if interaction.command is not None and interaction.command.name == "appeal":
            return None
        return await self.bot.db.get_active_ban(interaction.guild.id, user.id)

    async def _risk_track(self, interaction: discord.Interaction) -> None:
        """记录一次下载行为；超过风控阈值则自动封禁并通知。"""
        guild = interaction.guild
        if guild is None:
            return
        user = interaction.user
        if isinstance(user, discord.Member) and user.guild_permissions.administrator:
            return
        cfg = await self.bot.db.get_risk_config(guild.id)
        if not cfg["enabled"]:
            return
        await self.bot.db.record_download_event(guild.id, user.id)
        count = await self.bot.db.count_recent_downloads(
            guild.id, user.id, cfg["window_minutes"]
        )
        if count <= cfg["max_downloads"]:
            return
        reason = f"{cfg['window_minutes']} 分钟内下载 {count} 次，过于频繁"
        until = await self.bot.db.ban_user(guild.id, user.id, cfg["ban_hours"], reason)
        duration = fmt_duration(cfg["ban_hours"] * 3600)
        try:
            await interaction.followup.send(
                f"⛔ 检测到异常下载行为：{reason}。\n"
                f"你已被风控限制，**{duration}**内无法使用 Bot"
                f"（<t:{int(until)}:R> 解除）。如有异议可使用 `/appeal` 申诉。",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        await self.bot.log_admin(
            guild, user, f"⛔ 触发风控：{reason} → 封禁 {duration}"
        )

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
        # 风控：密码弹窗路径不经过指令检查，这里再拦一次
        ban = await self._check_ban(interaction)
        if ban is not None:
            await interaction.followup.send(
                "⛔ 你已被风控限制，暂时无法使用 Bot。", ephemeral=True
            )
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
            # 风控：统计下载频率，触发阈值则自动封禁
            await self._risk_track(interaction)
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

    # ────────────────────────── 风控申诉 ──────────────────────────

    @app_commands.command(
        name="appeal", description="风控申诉：提交解封工单，由管理员投票处理"
    )
    @app_commands.describe(reason="申诉理由（可选）")
    async def appeal(self, interaction: discord.Interaction, reason: str | None = None):
        assert interaction.guild is not None
        ban = await self.bot.db.get_active_ban(
            interaction.guild.id, interaction.user.id
        )
        if ban is None:
            await interaction.response.send_message(
                "✅ 你当前没有被风控限制。", ephemeral=True
            )
            return
        existing = await self.bot.db.get_open_appeal(
            interaction.guild.id, interaction.user.id
        )
        if existing is not None:
            await interaction.response.send_message(
                "⏳ 你已有待处理的申诉工单，请耐心等待管理员投票。", ephemeral=True
            )
            return
        settings = await self.bot.db.get_settings(interaction.guild.id)
        channel_id = (
            settings["ticket_channel_id"]
            if settings is not None and "ticket_channel_id" in settings.keys()
            else None
        )
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ 管理员尚未设置工单频道（`/ticket_channel`），请联系管理员处理。",
                ephemeral=True,
            )
            return

        # @所有管理员角色（找不到则退回 @here）
        admin_roles = [
            r
            for r in interaction.guild.roles
            if r.permissions.administrator and not r.managed and not r.is_default()
        ]
        mention = " ".join(r.mention for r in admin_roles[:10]) or "@here"

        view = AppealVoteView(
            self.bot,
            interaction.guild.id,
            interaction.user.id,
            ban["reason"],
            ban["banned_until"],
            (reason or "").strip(),
        )
        try:
            msg = await channel.send(
                content=mention, embed=view.initial_embed(interaction.user), view=view
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Bot 无权在 {channel.mention} 发送消息，请联系管理员检查权限。",
                ephemeral=True,
            )
            return
        view.message = msg
        view.appeal_id = await self.bot.db.create_appeal(
            interaction.guild.id, interaction.user.id, channel.id, msg.id
        )
        await interaction.response.send_message(
            f"✅ 申诉工单已提交到 {channel.mention}，请等待管理员投票处理。",
            ephemeral=True,
        )

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
