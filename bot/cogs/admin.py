"""管理员指令：存储模式设置、日志频道、审计查询、文件整理。"""
from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import STORAGE_CHANNEL_NAME, RepoBot, fmt_size

log = logging.getLogger("repo-bot")

admin_only = app_commands.checks.has_permissions(administrator=True)

# /organize 投票通过所需的管理员同意人数
ORGANIZE_VOTES_CATEGORY = 2
ORGANIZE_VOTES_GUILD = 3


class OrganizeVoteView(discord.ui.View):
    """整理投票：集齐所需数量的管理员同意后自动执行整理。"""

    def __init__(
        self,
        cog: "AdminCog",
        guild: discord.Guild,
        scope_label: str,
        channel_ids: set[int] | None,
        required: int,
        initiator: discord.Member,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.scope_label = scope_label
        self.channel_ids = channel_ids
        self.required = required
        self.initiator = initiator
        self.voters: set[int] = {initiator.id}
        self.message: discord.Message | None = None

    def make_embed(self, title: str, color: discord.Color | None = None) -> discord.Embed:
        voters = "、".join(f"<@{v}>" for v in self.voters)
        return discord.Embed(
            title=title,
            description=(
                f"范围：**{self.scope_label}**\n"
                f"发起者：{self.initiator.mention}（自动计 1 票）\n"
                f"所需同意：**{self.required}** 名管理员\n"
                f"当前票数：**{len(self.voters)}/{self.required}**　{voters}\n"
                "⏳ 投票 10 分钟内有效"
            ),
            color=color or discord.Color.blurple(),
        )

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="同意整理", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 只有管理员才能投票。", ephemeral=True)
            return
        if interaction.user.id in self.voters:
            await interaction.response.send_message("你已经投过同意了。", ephemeral=True)
            return
        self.voters.add(interaction.user.id)
        if len(self.voters) >= self.required:
            self._disable()
            await interaction.response.edit_message(
                embed=self.make_embed("✅ 投票通过，开始整理…", discord.Color.green()), view=self
            )
            self.stop()
            asyncio.create_task(self._run())
        else:
            await interaction.response.edit_message(embed=self.make_embed("🗂️ 整理投票"), view=self)

    @discord.ui.button(label="取消", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.initiator.id and (
            not isinstance(interaction.user, discord.Member)
            or not interaction.user.guild_permissions.administrator
        ):
            await interaction.response.send_message("❌ 只有发起者或管理员可以取消。", ephemeral=True)
            return
        self._disable()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="已取消",
                description="整理投票已被取消，未执行任何操作。",
                color=discord.Color.light_grey(),
            ),
            view=self,
        )
        self.stop()

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(
                    embed=discord.Embed(
                        title="投票超时",
                        description="10 分钟内未集齐同意票，整理已取消。",
                        color=discord.Color.light_grey(),
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass

    async def _run(self) -> None:
        self.cog._organizing.add(self.guild.id)
        try:
            stats = await self.cog.run_organize(self.guild, self.channel_ids, self.message)
            desc = (
                f"范围：**{self.scope_label}**\n"
                f"📦 已归拢文件：**{stats['moved']}** 个\n"
                f"⏭️ 无需移动：{stats['skipped']} 个"
            )
            if stats["failed"]:
                desc += f"\n⚠️ 失败 {stats['failed']} 个（详情见运行日志）"
            if self.message:
                await self.message.edit(
                    embed=discord.Embed(
                        title="🗂️ 整理完成", description=desc, color=discord.Color.green()
                    ),
                    view=None,
                )
        except Exception as exc:
            log.exception("整理执行失败")
            if self.message:
                try:
                    await self.message.edit(
                        embed=discord.Embed(
                            title="❌ 整理失败", description=str(exc), color=discord.Color.red()
                        ),
                        view=None,
                    )
                except discord.HTTPException:
                    pass
        finally:
            self.cog._organizing.discard(self.guild.id)


class AdminCog(commands.Cog, name="管理"):
    def __init__(self, bot: RepoBot):
        self.bot = bot
        self._organizing: set[int] = set()

    # ───────────────────── 存储模式 ─────────────────────

    @app_commands.command(name="storage_mode", description="设置文件存储方式（管理员）")
    @app_commands.describe(
        mode="category=在当前服务器子区存储；guild=存储到独立服务器",
        storage_guild_id="选择 guild 模式时填写存储服务器 ID（默认读取 .env 配置）",
        category="category 模式可选：直接使用你已建好的子区（分类），不填则自动新建",
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
        category: discord.CategoryChannel | None = None,
    ):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)

        if category is not None and mode.value != "category":
            await interaction.followup.send(
                "❌ 「category」参数仅适用于「当前服务器子区」模式。", ephemeral=True
            )
            return

        fields: dict = {"storage_mode": mode.value, "storage_channel_id": None}
        if category is not None:
            # 使用管理员指定的现有子区，不再新建
            channel = discord.utils.get(category.text_channels, name=STORAGE_CHANNEL_NAME)
            if channel is None:
                try:
                    channel = await category.create_text_channel(STORAGE_CHANNEL_NAME)
                except discord.Forbidden:
                    await interaction.followup.send(
                        f"❌ Bot 缺少权限，无法在子区「{category.name}」下创建存储频道。",
                        ephemeral=True,
                    )
                    return
            fields.update(
                storage_category_id=category.id, storage_channel_id=channel.id
            )
            await self.bot.db.upsert_settings(interaction.guild.id, **fields)
            await interaction.followup.send(
                f"✅ 存储模式已设置为 **当前服务器子区**\n"
                f"📁 存储位置：使用现有子区「**{category.name}**」→ {channel.mention}",
                ephemeral=True,
            )
            return

        if mode.value == "guild":
            guild_id: int | None = None
            if storage_guild_id and storage_guild_id.strip():
                if not storage_guild_id.strip().isdigit():
                    await interaction.followup.send(
                        "❌ 存储服务器 ID 格式错误：应为一串纯数字\n"
                        "（Discord 设置 → 高级 → 开启开发者模式后，右键服务器图标 → 复制服务器 ID）。",
                        ephemeral=True,
                    )
                    return
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

    # ───────────────────── 一键整理（投票制） ─────────────────────

    @app_commands.command(
        name="organize",
        description="一键整理：把散落的存储文件归拢到当前设置的存储频道（需管理员投票）",
    )
    @app_commands.describe(scope="整理范围")
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="当前子区（需 2 名管理员同意）", value="category"),
            app_commands.Choice(name="整个服务器（需 3 名管理员同意）", value="guild"),
        ]
    )
    @admin_only
    async def organize(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if interaction.guild.id in self._organizing:
            await interaction.response.send_message(
                "❌ 当前已有整理任务在执行，请等待其完成。", ephemeral=True
            )
            return

        if scope.value == "category":
            cat = (
                interaction.channel.category
                if isinstance(interaction.channel, discord.TextChannel)
                else None
            )
            if cat is None:
                await interaction.response.send_message(
                    "❌ 无法确定子区：请在目标子区（分类）内的文字频道中使用本指令。",
                    ephemeral=True,
                )
                return
            # 独立存储服务器模式：在存储服务器中按同名子区匹配
            settings = await self.bot.db.get_settings(interaction.guild.id)
            if settings and settings["storage_mode"] == "guild" and settings["storage_guild_id"]:
                storage_guild = self.bot.get_guild(settings["storage_guild_id"])
                target_cat = (
                    discord.utils.get(storage_guild.categories, name=cat.name)
                    if storage_guild
                    else None
                )
                if target_cat is None:
                    await interaction.response.send_message(
                        f"❌ 存储服务器中不存在同名子区「{cat.name}」。", ephemeral=True
                    )
                    return
                cat = target_cat
            channel_ids = {c.id for c in cat.text_channels}
            required = ORGANIZE_VOTES_CATEGORY
            scope_label = f"当前子区「{cat.name}」"
        else:
            channel_ids = None
            required = ORGANIZE_VOTES_GUILD
            scope_label = "整个服务器"

        view = OrganizeVoteView(
            self, interaction.guild, scope_label, channel_ids, required, interaction.user
        )
        await interaction.response.send_message(embed=view.make_embed("🗂️ 整理投票"), view=view)
        view.message = await interaction.original_response()

    async def run_organize(
        self,
        guild: discord.Guild,
        channel_ids: set[int] | None,
        message: discord.Message | None,
    ) -> dict:
        """把范围内文件的存储消息迁移到当前配置的存储频道，并更新索引。"""
        storage, error = await self.bot.resolve_storage_channel(guild)
        if storage is None:
            raise RuntimeError(f"存储频道不可用：{error}")

        files = await self.bot.db.list_all_files(guild.id)
        stats = {"moved": 0, "skipped": 0, "failed": 0}
        for i, row in enumerate(files, 1):
            if channel_ids is not None and row["storage_channel_id"] not in channel_ids:
                stats["skipped"] += 1
                continue
            if row["storage_channel_id"] == storage.id:
                stats["skipped"] += 1
                continue
            try:
                src = self.bot.get_channel(row["storage_channel_id"])
                if not isinstance(src, discord.TextChannel):
                    raise RuntimeError("原存储频道不存在")
                src_msg = await src.fetch_message(row["storage_message_id"])
                if not src_msg.attachments:
                    raise RuntimeError("原存储消息没有附件")
                data = await src_msg.attachments[0].read()
                new_msg = await storage.send(
                    embed=src_msg.embeds[0] if src_msg.embeds else None,
                    file=discord.File(io.BytesIO(data), filename=row["name"]),
                )
                await self.bot.db.update_file_storage(row["file_id"], storage.id, new_msg.id)
                await src_msg.delete()
                stats["moved"] += 1
            except Exception:
                log.exception("整理文件失败 file_id=%s", row["file_id"])
                stats["failed"] += 1
            if message is not None and i % 5 == 0:
                try:
                    await message.edit(
                        embed=discord.Embed(
                            title="🗂️ 正在整理…",
                            description=f"进度：**{i}/{len(files)}** 个文件",
                            color=discord.Color.blurple(),
                        )
                    )
                except discord.HTTPException:
                    pass
        return stats


async def setup(bot: RepoBot) -> None:
    await bot.add_cog(AdminCog(bot))
