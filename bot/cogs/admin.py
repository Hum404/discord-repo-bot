"""管理员指令：存储模式设置、日志频道、审计查询、文件整理。"""
from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import STORAGE_CHANNEL_NAME, RepoBot, fmt_size
from ..tracing import extract_trace
from .files import build_storage_embed

log = logging.getLogger("repo-bot")

admin_only = app_commands.checks.has_permissions(administrator=True)

# /organize 投票通过所需的管理员同意人数
ORGANIZE_VOTES_CATEGORY = 2
ORGANIZE_VOTES_GUILD = 3

# /reset_server 初始化通过所需的管理员同意人数
RESET_VOTES_REQUIRED = 3


class OrganizeVoteView(discord.ui.View):
    """整理投票：集齐所需数量的管理员同意后自动执行整理。"""

    def __init__(
        self,
        cog: "AdminCog",
        guild: discord.Guild,
        scope_label: str,
        channel_ids: set[int] | None,
        scan_channel_ids: set[int] | None,
        required: int,
        initiator: discord.Member,
        sweep_plan: dict[int, int] | None = None,
        misplaced: int = 0,
        filter_mode: str = "black",
        filter_count: int = 0,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.scope_label = scope_label
        self.channel_ids = channel_ids
        self.scan_channel_ids = scan_channel_ids
        self.required = required
        self.initiator = initiator
        self.sweep_plan = sweep_plan or {}
        self.misplaced = misplaced
        self.filter_mode = filter_mode
        self.filter_count = filter_count
        self.voters: set[int] = {initiator.id}
        self.message: discord.Message | None = None

    def make_embed(self, title: str, color: discord.Color | None = None) -> discord.Embed:
        voters = "、".join(f"<@{v}>" for v in self.voters)
        plan = f"📦 错位已登记文件待迁移：**{self.misplaced}** 个\n" if self.misplaced else ""
        if self.sweep_plan:
            total = sum(self.sweep_plan.values())
            top = sorted(self.sweep_plan.items(), key=lambda kv: -kv[1])[:5]
            names = []
            for cid, cnt in top:
                ch = self.guild.get_channel(cid)
                names.append(f"{ch.mention if ch else f'#{cid}'}（{cnt}）")
            plan += (
                f"🧹 待扫描频道 **{len(self.sweep_plan)}** 个，散落文件共 **{total}** 个：\n"
                + "、".join(names)
                + (" …" if len(self.sweep_plan) > 5 else "")
                + "\n"
            )
        else:
            plan += "🧹 未发现散落文件\n"
        if self.filter_count:
            plan += (
                f"🛡️ 已启用{'黑名单' if self.filter_mode == 'black' else '白名单'}"
                f"（{self.filter_count} 个频道，`/organize_filter` 修改）\n"
            )
        return discord.Embed(
            title=title,
            description=(
                f"范围：**{self.scope_label}**\n{plan}\n"
                f"发起者：{self.initiator.mention}（自动计 1 票）\n"
                f"所需同意：**{self.required}** 名管理员\n"
                f"当前票数：**{len(self.voters)}/{self.required}**　{voters}\n"
                "⏳ 投票 10 分钟内有效\n"
                "（投票通过后可进一步确认本次整理的频道范围）"
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
            self.stop()
            # 投票通过 → 交给管理员确认本次整理范围（可临时排除频道）
            confirm = OrganizeConfirmView(
                self.cog, self.guild, self.scope_label, self.channel_ids,
                self.scan_channel_ids, self.sweep_plan, self.misplaced, self.initiator,
            )
            await interaction.response.edit_message(embed=confirm.make_embed(), view=confirm)
            confirm.message = self.message
            await self.cog.bot.log_admin(
                self.guild, interaction.user,
                f"🗳️ 整理投票通过（{self.scope_label}），等待确认范围",
            )
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

class OrganizeConfirmView(discord.ui.View):
    """整理投票通过后：管理员确认本次整理范围（可临时排除频道或写入黑名单）。"""

    def __init__(
        self,
        cog: "AdminCog",
        guild: discord.Guild,
        scope_label: str,
        channel_ids: set[int] | None,
        scan_channel_ids: set[int] | None,
        sweep_plan: dict[int, int],
        misplaced: int,
        initiator: discord.Member,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.scope_label = scope_label
        self.channel_ids = channel_ids
        self.scan_channel_ids = scan_channel_ids
        self.sweep_plan = sweep_plan
        self.misplaced = misplaced
        self.initiator = initiator
        self.excluded: set[int] = set()
        self.message: discord.Message | None = None

        channels = [guild.get_channel(cid) for cid in sweep_plan]
        channels = [c for c in channels if c is not None][:25]
        if channels:
            select = discord.ui.ChannelSelect(
                placeholder="选择本次【跳过】的频道（可多选，默认全部整理）",
                channel_types=[discord.ChannelType.text],
                min_values=0,
                max_values=len(channels),
            )
            select.callback = self._on_select
            self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        if user.id == self.initiator.id:
            return True
        if isinstance(user, discord.Member) and user.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "❌ 只有管理员可以确认整理范围。", ephemeral=True
        )
        return False

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self.excluded = {int(v) for v in interaction.data.get("values", [])}
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    def make_embed(self) -> discord.Embed:
        desc = ""
        if self.misplaced:
            desc += f"📦 错位已登记文件待迁移：**{self.misplaced}** 个\n"
        total = 0
        lines = []
        for cid, count in sorted(self.sweep_plan.items(), key=lambda kv: -kv[1]):
            ch = self.guild.get_channel(cid)
            name = ch.mention if ch else f"#{cid}"
            if cid in self.excluded:
                lines.append(f"~~{name}~~ {count} 个（本次跳过）")
            else:
                lines.append(f"{name} — {count} 个文件")
                total += count
        if lines:
            shown = "\n".join(lines[:15])
            if len(lines) > 15:
                shown += f"\n… 共 {len(lines)} 个频道"
            desc += f"🧹 待扫描频道（散落文件共 **{total}** 个）：\n{shown}\n"
        if not desc:
            desc = "没有发现需要整理的文件。\n"
        if self.excluded:
            desc += f"\n🚫 本次跳过 **{len(self.excluded)}** 个频道"
        return discord.Embed(
            title="✅ 投票通过 — 确认本次整理范围",
            description=desc + "\n\n⏳ 请在 10 分钟内确认",
            color=discord.Color.green(),
        )

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="确认开始整理", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🗂️ 正在整理…", description="即将开始", color=discord.Color.blurple()
            ),
            view=self,
        )
        self.stop()
        await self._run()

    @discord.ui.button(label="排除并永久拉黑", emoji="🚫", style=discord.ButtonStyle.secondary)
    async def exclude_forever(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.excluded:
            await interaction.response.send_message(
                "请先在上方下拉框选择要跳过的频道。", ephemeral=True
            )
            return
        mode, ids = await self.cog.bot.db.get_organize_filter(self.guild.id)
        if mode == "white":
            ids -= self.excluded  # 白名单模式：从名单中移除
        else:
            ids |= self.excluded  # 黑名单模式：加入名单
        await self.cog.bot.db.set_organize_filter(self.guild.id, mode, ids)
        await interaction.response.send_message(
            f"🚫 已将 {len(self.excluded)} 个频道写入持久"
            f"{'黑名单' if mode == 'black' else '白名单'}，以后整理自动生效。",
            ephemeral=True,
        )

    @discord.ui.button(label="取消整理", emoji="❌", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="已取消",
                description="本次整理未执行任何操作。",
                color=discord.Color.light_grey(),
            ),
            view=self,
        )
        self.stop()

    async def _run(self) -> None:
        self.cog._organizing.add(self.guild.id)
        try:
            stats = await self.cog.run_organize(
                self.guild, self.channel_ids, self.scan_channel_ids, self.message,
                exclude_channel_ids=self.excluded,
            )
            desc = (
                f"范围：**{self.scope_label}**\n"
                f"📦 已归拢文件：**{stats['moved']}** 个\n"
                f"📝 新登记（本就在存储频道）：{stats['registered']} 个\n"
                f"⏭️ 无需移动：{stats['skipped']} 个"
            )
            if self.excluded:
                desc += f"\n🚫 本次跳过频道：{len(self.excluded)} 个"
            await self.cog.bot.log_admin(
                self.guild,
                self.initiator,
                f"🗂️ 整理完成（{self.scope_label}）：归拢 {stats['moved']} · "
                f"新登记 {stats['registered']} · 无需移动 {stats['skipped']} · "
                f"跳过频道 {len(self.excluded)} · 失败 {stats['failed']}",
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

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(
                    embed=discord.Embed(
                        title="确认超时",
                        description="10 分钟内未确认，本次整理已取消，未执行任何操作。",
                        color=discord.Color.light_grey(),
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass


class OrganizeFilterView(discord.ui.View):
    """一键整理的持久黑白名单配置（/organize_filter）。"""

    def __init__(
        self,
        bot,
        guild: discord.Guild,
        mode: str,
        channel_ids: set[int],
    ):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.mode = mode
        self.channel_ids = set(channel_ids)
        self.saved = False

        mode_select = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label="黑名单模式", value="black", emoji="🚫",
                    description="整理名单以外的所有频道（推荐）",
                    default=(mode == "black"),
                ),
                discord.SelectOption(
                    label="白名单模式", value="white", emoji="✅",
                    description="只整理名单内的频道",
                    default=(mode == "white"),
                ),
            ],
        )
        mode_select.callback = self._on_mode
        self.add_item(mode_select)

        ch_select = discord.ui.ChannelSelect(
            placeholder="选择名单频道（覆盖式，可多选，最多 25 个）",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=25,
        )
        ch_select.callback = self._on_channels
        self.add_item(ch_select)

    def make_embed(self) -> discord.Embed:
        mode_text = (
            "🚫 黑名单模式：整理【名单以外】的所有频道"
            if self.mode == "black"
            else "✅ 白名单模式：只整理【名单内】的频道"
        )
        if self.channel_ids:
            mentions = []
            for cid in sorted(self.channel_ids)[:25]:
                ch = self.guild.get_channel(cid)
                mentions.append(ch.mention if ch else f"`{cid}`（已删除）")
            ch_text = "、".join(mentions)
        else:
            ch_text = "（空）"
        embed = discord.Embed(
            title="🗂️ 整理黑白名单配置",
            description=(
                f"**模式**：{mode_text}\n\n**名单频道**：\n{ch_text}\n\n"
                "⚠️ 频道下拉框为覆盖式选择：重新选择会替换整个名单"
            ),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="修改后需点击「保存」生效" + (" · ✅ 已保存" if self.saved else ""))
        return embed

    async def _on_mode(self, interaction: discord.Interaction) -> None:
        self.mode = interaction.data["values"][0]
        self.saved = False
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_channels(self, interaction: discord.Interaction) -> None:
        self.channel_ids = {int(v) for v in interaction.data.get("values", [])}
        self.saved = False
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="保存", emoji="💾", style=discord.ButtonStyle.success, row=2)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.bot.db.set_organize_filter(
            self.guild.id, self.mode, self.channel_ids
        )
        self.saved = True
        await interaction.response.edit_message(embed=self.make_embed(), view=self)
        await interaction.followup.send("✅ 整理黑白名单已保存。", ephemeral=True)
        await self.bot.log_admin(
            interaction.guild, interaction.user,
            f"🗂️ 修改整理黑白名单：{self.mode} 模式，{len(self.channel_ids)} 个频道",
        )


class ResetVoteView(discord.ui.View):
    """初始化投票：集齐 3 名管理员同意后执行服务器初始化。"""

    def __init__(
        self,
        cog: "AdminCog",
        guild: discord.Guild,
        initiator: discord.Member,
        delete_files: bool,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.initiator = initiator
        self.delete_files = delete_files
        self.required = RESET_VOTES_REQUIRED
        self.voters: set[int] = {initiator.id}
        self.message: discord.Message | None = None

    @property
    def mode_label(self) -> str:
        return (
            "彻底重置（同时删除存储频道及其中所有文件）"
            if self.delete_files
            else "仅清空数据（保留存储频道，之后可 /organize 重新登记）"
        )

    def make_embed(self, title: str, color: discord.Color | None = None) -> discord.Embed:
        voters = "、".join(f"<@{v}>" for v in self.voters)
        return discord.Embed(
            title=title,
            description=(
                "⚠️ **高危操作，执行后不可撤销**\n"
                f"模式：**{self.mode_label}**\n"
                "将清空：全部文件记录、全部下载记录、服务器设置（存储/日志配置）\n"
                "执行后 Bot 恢复到刚加入服务器时的状态\n\n"
                f"发起者：{self.initiator.mention}（自动计 1 票）\n"
                f"所需同意：**{self.required}** 名管理员\n"
                f"当前票数：**{len(self.voters)}/{self.required}**　{voters}\n"
                "⏳ 投票 10 分钟内有效"
            ),
            color=color or discord.Color.red(),
        )

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="同意初始化", emoji="⚠️", style=discord.ButtonStyle.danger)
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
                embed=self.make_embed("⚠️ 投票通过，开始初始化…"), view=self
            )
            self.stop()
            asyncio.create_task(self._run())
        else:
            await interaction.response.edit_message(
                embed=self.make_embed("⚠️ 初始化投票"), view=self
            )

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
                description="初始化投票已被取消，未执行任何操作。",
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
                        description="10 分钟内未集齐同意票，初始化已取消。",
                        color=discord.Color.light_grey(),
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass

    async def _run(self) -> None:
        self.cog._organizing.add(self.guild.id)  # 复用执行锁，避免与整理并发
        try:
            stats = await self.cog.run_reset(self.guild, self.delete_files)
            desc = (
                f"模式：**{self.mode_label}**\n"
                f"🗑️ 文件记录：已清除 **{stats['files']}** 条\n"
                f"🧾 下载记录：已清除 **{stats['downloads']}** 条\n"
                "⚙️ 服务器设置：已清除\n"
                f"📁 存储频道：{'已删除' if stats['channel_deleted'] else '未删除'}\n\n"
                "✅ Bot 已恢复到刚加入服务器时的状态，下次 `/upload` 将重新初始化存储。"
            )
            if self.message:
                try:
                    await self.message.edit(
                        embed=discord.Embed(
                            title="✅ 初始化完成", description=desc, color=discord.Color.green()
                        ),
                        view=None,
                    )
                except discord.HTTPException:
                    pass  # 投票消息所在频道可能已随存储频道一起被删除
        except Exception as exc:
            log.exception("初始化执行失败")
            if self.message:
                try:
                    await self.message.edit(
                        embed=discord.Embed(
                            title="❌ 初始化失败", description=str(exc), color=discord.Color.red()
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
            await self.bot.log_admin(
                interaction.guild,
                interaction.user,
                f"⚙️ 存储模式 → 当前服务器子区（「{category.name}」→ {channel.mention}）",
            )
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
        await self.bot.log_admin(
            interaction.guild,
            interaction.user,
            f"⚙️ 存储模式 → {mode_text}（{storage.guild.name} → #{storage.name}）",
        )
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
        await self.bot.log_admin(
            interaction.guild,
            interaction.user,
            f"⚙️ 审计日志频道 → {channel.mention if channel else '关闭'}",
        )
        if channel:
            await interaction.response.send_message(
                f"✅ 审计日志将发送到 {channel.mention}。", ephemeral=True
            )
        else:
            await interaction.response.send_message("✅ 已关闭审计日志。", ephemeral=True)

    @app_commands.command(name="admin_log_channel", description="设置管理日志频道（管理员）")
    @app_commands.describe(channel="管理日志发送到的频道；不填则跟随审计日志频道")
    @admin_only
    async def admin_log_channel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ):
        assert interaction.guild is not None
        await self.bot.db.upsert_settings(
            interaction.guild.id, admin_log_channel_id=channel.id if channel else None
        )
        await self.bot.log_admin(
            interaction.guild,
            interaction.user,
            f"⚙️ 管理日志频道 → {channel.mention if channel else '跟随审计日志频道'}",
        )
        if channel:
            await interaction.response.send_message(
                f"✅ 管理日志将发送到 {channel.mention}。", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "✅ 管理日志已改为跟随审计日志频道。", ephemeral=True
            )

    # ───────────────────── 用户下载审计 ─────────────────────

    @app_commands.command(name="audit_user", description="查看某成员的下载记录（管理员）")
    @app_commands.describe(member="要审计的成员")
    @admin_only
    async def audit_user(
        self, interaction: discord.Interaction, member: discord.Member
    ):
        assert interaction.guild is not None
        await self.bot.log_admin(
            interaction.guild,
            interaction.user,
            f"🕵️ 查询成员下载记录：{member} (`{member.id}`)",
        )
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

    # ───────────────────── 文件溯源反查 ─────────────────────

    @app_commands.command(name="trace", description="溯源查询：读取疑似外泄文件中的下载标记（管理员）")
    @app_commands.describe(file="要检查的文件（把外泄的文件传上来）")
    @admin_only
    async def trace(self, interaction: discord.Interaction, file: discord.Attachment):
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True)
        if file.size > 50 * 1024 * 1024:
            await interaction.followup.send("❌ 文件过大，无法检查。", ephemeral=True)
            return
        try:
            data = await asyncio.wait_for(file.read(), timeout=120)
        except Exception:
            await interaction.followup.send(
                "❌ 读取附件失败（Discord CDN 连接异常或超时），请重试。", ephemeral=True
            )
            return

        result = extract_trace(data, file.filename)
        if result[0] == "hit":
            _, uid, ts = result
            desc = (
                f"🎯 **溯源命中！**\n"
                f"下载者：<@{uid}>（`{uid}`）\n"
                f"下载时间：<t:{ts}:F>"
            )
            action = f"🔍 溯源 `{file.filename}` → 命中 <@{uid}>"
        elif result[0] == "image":
            desc = "该文件是图片：水印为可见文字，请直接查看图片**右下角**的 ID 和时间。"
            action = f"🔍 溯源 `{file.filename}` → 图片水印（需人工查看）"
        else:
            desc = (
                "未在该文件中发现溯源标记。\n"
                "可能原因：文件类型不支持标记、标记已被破坏（重打包/转码/截图），"
                "或文件不是从本仓库下载的。"
            )
            action = f"🔍 溯源 `{file.filename}` → 未命中"
        await self.bot.log_admin(interaction.guild, interaction.user, action)
        await interaction.followup.send(
            embed=discord.Embed(
                title="🔍 文件溯源", description=desc, color=discord.Color.gold()
            ),
            ephemeral=True,
        )

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
            business_cat = (
                interaction.channel.category
                if isinstance(interaction.channel, discord.TextChannel)
                else None
            )
            if business_cat is None:
                await interaction.response.send_message(
                    "❌ 无法确定子区：请在目标子区（分类）内的文字频道中使用本指令。",
                    ephemeral=True,
                )
                return
            # 独立存储服务器模式：在存储服务器中按同名子区匹配
            settings = await self.bot.db.get_settings(interaction.guild.id)
            cat = business_cat
            if settings and settings["storage_mode"] == "guild" and settings["storage_guild_id"]:
                storage_guild = self.bot.get_guild(settings["storage_guild_id"])
                target_cat = (
                    discord.utils.get(storage_guild.categories, name=business_cat.name)
                    if storage_guild
                    else None
                )
                if target_cat is None:
                    await interaction.response.send_message(
                        f"❌ 存储服务器中不存在同名子区「{business_cat.name}」。", ephemeral=True
                    )
                    return
                cat = target_cat
            # 迁移过滤用存储侧频道；扫描散落文件用业务侧频道
            channel_ids = {c.id for c in cat.text_channels}
            scan_channel_ids = {c.id for c in business_cat.text_channels}
            required = ORGANIZE_VOTES_CATEGORY
            scope_label = f"当前子区「{business_cat.name}」"
        else:
            channel_ids = None
            scan_channel_ids = None
            required = ORGANIZE_VOTES_GUILD
            scope_label = "整个服务器"

        # ── 预扫描：统计本次将处理的文件，供投票与范围确认参考 ──
        await interaction.response.defer()
        progress = await interaction.followup.send("🔍 正在预扫描整理范围…")

        storage, storage_err = await self.bot.resolve_storage_channel(guild)
        if storage is None:
            await progress.edit(content=f"❌ 存储频道不可用：{storage_err}")
            return

        all_files = await self.bot.db.list_all_files(guild.id)
        misplaced = sum(
            1
            for r in all_files
            if r["storage_channel_id"] != storage.id
            and (channel_ids is None or r["storage_channel_id"] in channel_ids)
        )
        tracked = {(r["storage_channel_id"], r["storage_message_id"]) for r in all_files}

        # 候选扫描频道（与 run_organize 阶段 2 同一口径），套用持久黑白名单
        if scan_channel_ids is None:
            candidates: list = list(guild.text_channels)
        else:
            candidates = [
                ch
                for cid in scan_channel_ids
                if isinstance((ch := guild.get_channel(cid)), discord.TextChannel)
            ]
        filter_mode, filter_ids = await self.bot.db.get_organize_filter(guild.id)
        if filter_ids:
            if filter_mode == "white":
                candidates = [c for c in candidates if c.id in filter_ids]
            else:
                candidates = [c for c in candidates if c.id not in filter_ids]

        # 逐频道统计未登记的文件消息（仅看最近 100 条，作为预览）
        me = self.bot.user
        sweep_plan: dict[int, int] = {}
        for ch in candidates:
            try:
                count = 0
                async for msg in ch.history(limit=100):
                    if not msg.attachments or (me is not None and msg.author.id == me.id):
                        continue
                    if (ch.id, msg.id) in tracked:
                        continue
                    count += len(msg.attachments)
                if count:
                    sweep_plan[ch.id] = count
            except discord.Forbidden:
                continue

        view = OrganizeVoteView(
            self, interaction.guild, scope_label, channel_ids, scan_channel_ids,
            required, interaction.user,
            sweep_plan=sweep_plan, misplaced=misplaced,
            filter_mode=filter_mode, filter_count=len(filter_ids),
        )
        await progress.edit(content=None, embed=view.make_embed("🗂️ 整理投票"), view=view)
        view.message = progress
        await self.bot.log_admin(
            interaction.guild, interaction.user, f"🗳️ 发起整理投票（{scope_label}）"
        )

    @app_commands.command(
        name="organize_filter",
        description="配置一键整理的频道黑白名单（永久生效，管理员）",
    )
    @admin_only
    async def organize_filter(self, interaction: discord.Interaction):
        assert interaction.guild is not None
        mode, channel_ids = await self.bot.db.get_organize_filter(interaction.guild.id)
        view = OrganizeFilterView(self.bot, interaction.guild, mode, channel_ids)
        await interaction.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)

    async def run_organize(
        self,
        guild: discord.Guild,
        channel_ids: set[int] | None,
        scan_channel_ids: set[int] | None,
        message: discord.Message | None,
        exclude_channel_ids: set[int] | None = None,
    ) -> dict:
        """整理存储文件，分两阶段：

        ① 迁移：已登记但不在当前存储频道的文件，搬运到存储频道并更新索引；
        ② 扫描：范围内频道里未登记的文件消息（成员直接发的），登记入库并
           搬入存储频道；本就在存储频道的单附件消息原地登记。
        """
        storage, error = await self.bot.resolve_storage_channel(guild)
        if storage is None:
            raise RuntimeError(f"存储频道不可用：{error}")

        files = await self.bot.db.list_all_files(guild.id)
        tracked = {(r["storage_channel_id"], r["storage_message_id"]) for r in files}
        stats = {"moved": 0, "registered": 0, "skipped": 0, "failed": 0}
        size_limit = self.bot.config.max_file_size_mb * 1024 * 1024

        async def report_progress(text: str) -> None:
            if message is None:
                return
            try:
                await message.edit(
                    embed=discord.Embed(
                        title="🗂️ 正在整理…",
                        description=text,
                        color=discord.Color.blurple(),
                    )
                )
            except discord.HTTPException:
                pass

        # ── 阶段 1：迁移已登记但不在存储频道的文件 ──
        total = len(files)
        for i, row in enumerate(files, 1):
            if channel_ids is not None and row["storage_channel_id"] not in channel_ids:
                stats["skipped"] += 1
                continue
            if exclude_channel_ids and row["storage_channel_id"] in exclude_channel_ids:
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
                tracked.add((storage.id, new_msg.id))
                await src_msg.delete()
                stats["moved"] += 1
            except Exception:
                log.exception("整理文件失败 file_id=%s", row["file_id"])
                stats["failed"] += 1
            if i % 5 == 0:
                await report_progress(f"阶段 1/2 迁移已登记文件：**{i}/{total}**")

        # ── 阶段 2：扫描范围内频道，登记未入库的散落文件 ──
        if scan_channel_ids is None:
            scan_channels: list[discord.TextChannel] = list(guild.text_channels)
        else:
            scan_channels = [
                ch
                for cid in scan_channel_ids
                if isinstance((ch := guild.get_channel(cid)), discord.TextChannel)
            ]

        # 套用持久黑白名单（以执行时配置为准）+ 本次确认的排除频道
        filter_mode, filter_ids = await self.bot.db.get_organize_filter(guild.id)
        if filter_ids:
            if filter_mode == "white":
                scan_channels = [c for c in scan_channels if c.id in filter_ids]
            else:
                scan_channels = [c for c in scan_channels if c.id not in filter_ids]
        if exclude_channel_ids:
            scan_channels = [c for c in scan_channels if c.id not in exclude_channel_ids]

        me = self.bot.user
        scanned = 0
        for ch in scan_channels:
            try:
                async for msg in ch.history(limit=None, oldest_first=True):
                    if not msg.attachments or (me is not None and msg.author.id == me.id):
                        continue
                    if (ch.id, msg.id) in tracked:
                        continue
                    # 本就在存储频道的单附件消息：原地登记，无需搬运
                    in_place = ch.id == storage.id and len(msg.attachments) == 1
                    all_ok = True
                    for att in msg.attachments:
                        if att.size > size_limit:
                            log.warning(
                                "整理跳过超大文件：%s (%s B)", att.filename, att.size
                            )
                            stats["failed"] += 1
                            all_ok = False
                            continue
                        try:
                            data = await att.read()
                            description = (msg.content or "").strip()[:200]
                            if in_place:
                                await self.bot.db.add_file(
                                    origin_guild_id=guild.id,
                                    name=att.filename,
                                    size=att.size,
                                    content_type=att.content_type,
                                    description=description,
                                    uploader_id=msg.author.id,
                                    uploader_name=str(msg.author),
                                    storage_channel_id=ch.id,
                                    storage_message_id=msg.id,
                                    uploaded_at=int(msg.created_at.timestamp()),
                                )
                                stats["registered"] += 1
                            else:
                                embed = build_storage_embed(
                                    att.filename, att.size, msg.author, description
                                )
                                new_msg = await storage.send(
                                    embed=embed,
                                    file=discord.File(
                                        io.BytesIO(data), filename=att.filename
                                    ),
                                )
                                file_id, seq = await self.bot.db.add_file(
                                    origin_guild_id=guild.id,
                                    name=att.filename,
                                    size=att.size,
                                    content_type=att.content_type,
                                    description=description,
                                    uploader_id=msg.author.id,
                                    uploader_name=str(msg.author),
                                    storage_channel_id=storage.id,
                                    storage_message_id=new_msg.id,
                                    uploaded_at=int(msg.created_at.timestamp()),
                                )
                                embed.set_footer(
                                    text=f"编号 #{seq} · 文件 ID：{file_id}"
                                )
                                try:
                                    await new_msg.edit(embed=embed)
                                except discord.HTTPException:
                                    pass
                                tracked.add((storage.id, new_msg.id))
                                stats["moved"] += 1
                        except Exception:
                            log.exception("整理登记文件失败 msg=%s", msg.id)
                            stats["failed"] += 1
                            all_ok = False
                    # 附件全部入库后删除原消息（原地登记的除外）
                    if not in_place and all_ok:
                        try:
                            await msg.delete()
                        except discord.HTTPException:
                            pass
                    tracked.add((ch.id, msg.id))
                    scanned += 1
                    if scanned % 10 == 0:
                        await report_progress(
                            f"阶段 2/2 扫描散落文件：已处理 **{scanned}** 条消息\n"
                            f"已归拢 {stats['moved']} · 新登记 {stats['registered']} · "
                            f"失败 {stats['failed']}"
                        )
            except discord.Forbidden:
                log.warning("整理：无权读取频道 #%s 的历史，已跳过", ch.name)
        return stats

    # ───────────────────── 初始化本服务器（投票制） ─────────────────────

    @app_commands.command(
        name="reset_server",
        description="初始化本服务器：清空数据，Bot 恢复到刚加入时的状态（需 3 名管理员同意）",
    )
    @app_commands.describe(mode="初始化模式")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="仅清空数据（保留存储频道，之后可重新登记）", value="data"),
            app_commands.Choice(name="彻底重置（同时删除存储频道及其中所有文件）", value="full"),
        ]
    )
    @admin_only
    async def reset_server(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        if interaction.guild.id in self._organizing:
            await interaction.response.send_message(
                "❌ 当前已有整理/初始化任务在执行，请等待其完成。", ephemeral=True
            )
            return
        view = ResetVoteView(
            self, interaction.guild, interaction.user, delete_files=(mode.value == "full")
        )
        await interaction.response.send_message(
            embed=view.make_embed("⚠️ 初始化投票"), view=view
        )
        view.message = await interaction.original_response()
        await self.bot.log_admin(
            interaction.guild, interaction.user, f"⚠️ 发起初始化投票（{view.mode_label}）"
        )

    async def run_reset(self, guild: discord.Guild, delete_files: bool) -> dict:
        """执行初始化：可选删除存储频道，然后清空本服全部数据与设置。"""
        settings = await self.bot.db.get_settings(guild.id)

        # 设置即将被清除，先拿到日志频道对象，完事后再发最后一条管理日志
        log_ch = None
        if settings:
            cid = settings["admin_log_channel_id"] or settings["log_channel_id"]
            if cid:
                ch = guild.get_channel(cid)
                if isinstance(ch, discord.TextChannel):
                    log_ch = ch

        channel_deleted = False
        if delete_files and settings and settings["storage_channel_id"]:
            ch = self.bot.get_channel(settings["storage_channel_id"])
            if isinstance(ch, discord.TextChannel):
                cat = ch.category
                try:
                    await ch.delete()
                    channel_deleted = True
                except (discord.Forbidden, discord.HTTPException):
                    log.warning("初始化：删除存储频道失败", exc_info=True)
                # Bot 自动创建的专用分类空了一并删除；管理员自建的分类不动
                if (
                    channel_deleted
                    and cat is not None
                    and cat.name == STORAGE_CATEGORY_NAME
                    and len(cat.channels) == 0
                ):
                    try:
                        await cat.delete()
                    except (discord.Forbidden, discord.HTTPException):
                        pass

        stats = await self.bot.db.purge_guild(guild.id)
        stats["channel_deleted"] = channel_deleted

        if log_ch is not None:
            try:
                await log_ch.send(
                    embed=discord.Embed(
                        title="🛡️ 管理日志",
                        description=(
                            f"⚠️ **服务器已初始化**\n"
                            f"清除文件记录 {stats['files']} 条 · 下载记录 {stats['downloads']} 条\n"
                            f"{'存储频道已删除' if channel_deleted else '存储频道已保留'}\n"
                            "本日志频道的绑定已随设置一并清除，此为最后一条管理日志。"
                        ),
                        color=discord.Color.dark_gold(),
                    )
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
        return stats


async def setup(bot: RepoBot) -> None:
    await bot.add_cog(AdminCog(bot))
