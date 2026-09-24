"""管理员指令：存储模式设置、日志频道、审计查询、文件整理。"""
from __future__ import annotations

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import STORAGE_CATEGORY_NAME, STORAGE_CHANNEL_NAME, RepoBot, fmt_size
from .files import (
    PERMANENT_BAN_HOURS,
    build_storage_embed,
    fmt_duration,
)

log = logging.getLogger("repo-bot")

admin_only = app_commands.checks.has_permissions(administrator=True)

# 各类管理员投票的默认所需同意人数（可用 /vote_config 按服务器调整，范围 1~20）
DEFAULT_ORGANIZE_VOTES_CATEGORY = 2
DEFAULT_ORGANIZE_VOTES_GUILD = 3
DEFAULT_RESET_VOTES_REQUIRED = 3


class OrganizeCancelled(Exception):
    """整理/初始化任务被 /organize_cancel 手动取消。"""


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
        filter_ids: set[int] | None = None,
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
        self.filter_ids = filter_ids or set()
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
        if self.filter_ids:
            plan += (
                f"🛡️ 默认名单：{'黑名单' if self.filter_mode == 'black' else '白名单'}"
                f"（{len(self.filter_ids)} 个频道，通过后可调整）\n"
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
                filter_mode=self.filter_mode, filter_channels=self.filter_ids,
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
    """整理投票通过后：管理员确认本次整理范围。

    可选「黑名单/白名单」模式 + 名单频道（默认载入持久配置），
    本次生效；也可一键保存为默认配置，以后投票通过后可再修改。
    """

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
        filter_mode: str = "black",
        filter_channels: set[int] | None = None,
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
        self.mode = filter_mode if filter_mode in ("black", "white") else "black"
        # 名单频道：只保留本次待扫描范围内的（其余对本次整理无意义）
        self.channels: set[int] = {
            cid for cid in (filter_channels or set()) if cid in sweep_plan
        }
        # 是否登记散落文件（成员直接发到频道、未入库的文件）；关闭后仅迁移错位文件
        self.sweep = True
        self.message: discord.Message | None = None

        mode_select = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label="黑名单模式", value="black", emoji="🚫",
                    description="整理【所选以外】的频道",
                    default=(self.mode == "black"),
                ),
                discord.SelectOption(
                    label="白名单模式", value="white", emoji="✅",
                    description="只整理【所选】的频道",
                    default=(self.mode == "white"),
                ),
            ],
        )
        mode_select.callback = self._on_mode
        self.add_item(mode_select)

        channels = [guild.get_channel(cid) for cid in sweep_plan]
        channels = [c for c in channels if c is not None][:25]
        if channels:
            select = discord.ui.ChannelSelect(
                placeholder="选择名单频道（可多选）",
                channel_types=[discord.ChannelType.text],
                min_values=0,
                max_values=len(channels),
                default_values=[c for c in channels if c.id in self.channels],
            )
            select.callback = self._on_channels
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

    async def _on_mode(self, interaction: discord.Interaction) -> None:
        self.mode = interaction.data["values"][0]
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    async def _on_channels(self, interaction: discord.Interaction) -> None:
        self.channels = {int(v) for v in interaction.data.get("values", [])}
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    def _effective_ids(self) -> set[int]:
        """本次实际扫描的频道。"""
        plan = set(self.sweep_plan)
        if self.mode == "white":
            return plan & self.channels
        return plan - self.channels

    def make_embed(self) -> discord.Embed:
        effective = self._effective_ids()
        desc = ""
        if self.misplaced:
            desc += f"📦 错位已登记文件待迁移：**{self.misplaced}** 个\n"
        mode_text = (
            "🚫 黑名单：整理【所选以外】的频道"
            if self.mode == "black"
            else "✅ 白名单：只整理【所选】的频道"
        )
        desc += f"🛡️ 名单模式：{mode_text}\n"
        desc += (
            "🧹 散落文件登记：开启（范围内未登记的文件消息会入库并归拢）\n"
            if self.sweep
            else "🧹 散落文件登记：**关闭**（只迁移错位的已登记文件，不扫描频道）\n"
        )
        total = 0
        lines = []
        for cid, count in sorted(self.sweep_plan.items(), key=lambda kv: -kv[1]):
            ch = self.guild.get_channel(cid)
            name = ch.mention if ch else f"#{cid}"
            if cid in effective:
                lines.append(f"{name} — {count} 个文件")
                total += count
            else:
                lines.append(f"~~{name}~~ {count} 个（跳过）")
        if not self.sweep:
            total_all = sum(self.sweep_plan.values())
            if total_all:
                desc += f"🧹 范围内有 {total_all} 个散落文件，本次**不会登记**\n"
        elif lines:
            shown = "\n".join(lines[:15])
            if len(lines) > 15:
                shown += f"\n… 共 {len(lines)} 个频道"
            desc += f"🧹 本次将扫描（散落文件共 **{total}** 个）：\n{shown}\n"
        elif not self.sweep_plan:
            desc += "🧹 没有发现散落文件\n"
        else:
            desc += "🧹 当前名单下没有要扫描的频道\n"
        return discord.Embed(
            title="✅ 投票通过 — 确认本次整理范围",
            description=desc + "\n⏳ 请在 10 分钟内确认",
            color=discord.Color.green(),
        )

    def _disable(self) -> None:
        for child in self.children:
            child.disabled = True

    async def _start(self, interaction: discord.Interaction, *, save: bool) -> None:
        if save:
            await self.cog.bot.db.set_organize_filter(
                self.guild.id, self.mode, self.channels
            )
        self._disable()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🗂️ 正在整理…", description="即将开始", color=discord.Color.blurple()
            ),
            view=self,
        )
        self.stop()
        await self._run(saved=save)

    @discord.ui.button(label="散落文件登记：开", emoji="🧹", style=discord.ButtonStyle.secondary)
    async def toggle_sweep(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.sweep = not self.sweep
        button.label = f"散落文件登记：{'开' if self.sweep else '关'}"
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="确认开始整理", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._start(interaction, save=False)

    @discord.ui.button(label="确认并保存为默认", emoji="💾", style=discord.ButtonStyle.primary)
    async def confirm_save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._start(interaction, save=True)

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

    async def _run(self, *, saved: bool) -> None:
        self.cog._organize_cancel.discard(self.guild.id)  # 清掉可能残留的取消标记
        self.cog._organizing.add(self.guild.id)
        try:
            # 白名单 = 只扫描所选；黑名单 = 扫描所选以外
            if self.mode == "white":
                include: set[int] | None = self._effective_ids()
                exclude: set[int] | None = None
            else:
                include = None
                exclude = set(self.channels)
            stats = await self.cog.run_organize(
                self.guild, self.channel_ids, self.scan_channel_ids, self.message,
                exclude_channel_ids=exclude, include_channel_ids=include,
                sweep_loose=self.sweep,
            )
            mode_text = "白名单" if self.mode == "white" else "黑名单"
            desc = (
                f"范围：**{self.scope_label}**\n"
                f"📦 已归拢文件：**{stats['moved']}** 个\n"
                f"📝 新登记（本就在存储频道）：{stats['registered']} 个\n"
                f"⏭️ 无需移动：{stats['skipped']} 个\n"
                f"🛡️ 名单：{mode_text} {len(self.channels)} 个频道"
            )
            if saved:
                desc += "（已保存为默认配置）"
            await self.cog.bot.log_admin(
                self.guild,
                self.initiator,
                f"🗂️ 整理完成（{self.scope_label}）：归拢 {stats['moved']} · "
                f"新登记 {stats['registered']} · 无需移动 {stats['skipped']} · "
                f"{mode_text}名单 {len(self.channels)} 个频道 · 失败 {stats['failed']}",
            )
            if stats["failed"]:
                desc += f"\n⚠️ 失败 {stats['failed']} 个（详情见运行日志 logs/bot.log）"
            if self.message:                await self.message.edit(
                    embed=discord.Embed(
                        title="🗂️ 整理完成", description=desc, color=discord.Color.green()
                    ),
                    view=None,
                )
        except OrganizeCancelled:
            await self.cog.bot.log_admin(
                self.guild, self.initiator,
                f"⏹️ 整理被手动取消（{self.scope_label}）",
            )
            if self.message:
                try:
                    await self.message.edit(
                        embed=discord.Embed(
                            title="⏹️ 整理已取消",
                            description="任务被 /organize_cancel 手动取消，已处理的部分不回滚。",
                            color=discord.Color.light_grey(),
                        ),
                        view=None,
                    )
                except discord.HTTPException:
                    pass
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
            self.cog._organize_cancel.discard(self.guild.id)

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


class RiskConfigModal(discord.ui.Modal, title="🛡️ 风控参数设置"):
    """风控参数弹窗：统计窗口 / 次数上限 / 封禁时长 / 管理员处理时限。"""

    def __init__(self, view: "RiskConfigView"):
        super().__init__()
        self.view = view
        cfg = view.cfg
        self.window = discord.ui.TextInput(
            label="统计窗口（分钟）", default=str(cfg["window_minutes"]),
            min_length=1, max_length=5,
        )
        self.max_dl = discord.ui.TextInput(
            label="窗口内下载次数上限", default=str(cfg["max_downloads"]),
            min_length=1, max_length=5,
        )
        self.hours = discord.ui.TextInput(
            label="封禁时长（小时，可填 0.5）", default=f"{cfg['ban_hours']:g}",
            min_length=1, max_length=6,
        )
        self.review_minutes = discord.ui.TextInput(
            label="管理员处理时限（分钟，仅通知模式）",
            default=str(cfg["review_minutes"]),
            min_length=1, max_length=5,
        )
        self.add_item(self.window)
        self.add_item(self.max_dl)
        self.add_item(self.hours)
        self.add_item(self.review_minutes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            window = int(self.window.value)
            max_dl = int(self.max_dl.value)
            hours = float(self.hours.value)
            review_minutes = int(self.review_minutes.value)
            if window < 1 or max_dl < 1 or hours <= 0 or review_minutes < 1:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ 参数格式不正确：窗口、次数与处理时限需为正整数，封禁时长为正数。",
                ephemeral=True,
            )
            return
        await self.view.cog.bot.db.set_risk_config(
            interaction.guild.id,
            window_minutes=window, max_downloads=max_dl, ban_hours=hours,
            review_minutes=review_minutes,
        )
        self.view.cfg = await self.view.cog.bot.db.get_risk_config(interaction.guild.id)
        await interaction.response.edit_message(
            embed=self.view.make_embed(), view=self.view
        )
        await self.view.cog.bot.log_admin(
            interaction.guild, interaction.user,
            f"🛡️ 修改风控参数：{window} 分钟内最多 {max_dl} 次下载，触发封禁 {hours:g} 小时，"
            f"通知模式下管理员处理时限 {review_minutes} 分钟",
        )


class RiskConfigView(discord.ui.View):
    """下载风控配置面板（/risk_config）。"""

    def __init__(self, cog: "AdminCog", cfg: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.cfg = dict(cfg)

        toggle = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label="启用风控", value="on", emoji="✅",
                    description="成员短时间多次下载将被限制",
                    default=cfg["enabled"],
                ),
                discord.SelectOption(
                    label="关闭风控", value="off", emoji="❌",
                    default=not cfg["enabled"],
                ),
            ],
            row=0,
        )
        toggle.callback = self._on_toggle
        self.add_item(toggle)

        mode = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label="触发后自动封禁", value="auto", emoji="⛔",
                    description="检测到异常立即封禁",
                    default=cfg["action_mode"] == "auto",
                ),
                discord.SelectOption(
                    label="触发后通知管理员处理", value="review", emoji="🚨",
                    description="发工单给管理员，超时未处理自动封禁",
                    default=cfg["action_mode"] == "review",
                ),
            ],
            row=1,
        )
        mode.callback = self._on_mode
        self.add_item(mode)

    def make_embed(self) -> discord.Embed:
        cfg = self.cfg
        status = "✅ 已启用" if cfg["enabled"] else "❌ 已关闭"
        if cfg["action_mode"] == "review":
            action_text = (
                "🚨 **通知管理员处理**：发送待处理工单（工单频道优先，未设置则\n"
                "退回管理/审计日志频道），管理员可「立即封禁」或「放行」；\n"
                f"若 **{cfg['review_minutes']}** 分钟内未处理 → 自动封禁 "
                f"**{cfg['ban_hours']:g}** 小时"
            )
        else:
            action_text = f"⛔ **自动封禁**：立即封禁 **{cfg['ban_hours']:g}** 小时"
        return discord.Embed(
            title="🛡️ 下载风控配置",
            description=(
                f"状态：**{status}**\n\n"
                f"成员在 **{cfg['window_minutes']}** 分钟内下载超过 "
                f"**{cfg['max_downloads']}** 次即触发风控\n\n"
                f"触发后处置：{action_text}\n\n"
                "被封禁成员无法使用 Bot 任何指令（`/appeal` 申诉除外），到期自动解封。\n"
                "被风控成员可提交申诉工单，由管理员投票解封（`/ticket_channel` 设置工单频道）。\n"
                "管理员不受风控限制。"
            ),
            color=discord.Color.blurple(),
        )

    async def _on_toggle(self, interaction: discord.Interaction) -> None:
        enabled = interaction.data["values"][0] == "on"
        await self.cog.bot.db.set_risk_config(interaction.guild.id, enabled=enabled)
        self.cfg = await self.cog.bot.db.get_risk_config(interaction.guild.id)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)
        await self.cog.bot.log_admin(
            interaction.guild, interaction.user,
            f"🛡️ {'启用' if enabled else '关闭'}下载风控",
        )

    async def _on_mode(self, interaction: discord.Interaction) -> None:
        mode = interaction.data["values"][0]
        await self.cog.bot.db.set_risk_config(interaction.guild.id, action_mode=mode)
        self.cfg = await self.cog.bot.db.get_risk_config(interaction.guild.id)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)
        await self.cog.bot.log_admin(
            interaction.guild, interaction.user,
            f"🛡️ 风控处置方式 → "
            f"{'自动封禁' if mode == 'auto' else '通知管理员处理（超时未处理自动封禁）'}",
        )

    @discord.ui.button(label="修改参数", emoji="✏️", style=discord.ButtonStyle.primary, row=2)
    async def edit_params(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RiskConfigModal(self))


class VoteConfigModal(discord.ui.Modal, title="🗳️ 投票人数设置"):
    """各类管理员投票所需的同意人数（1~20）。"""

    def __init__(self, view: "VoteConfigView"):
        super().__init__()
        self.view = view
        cfg = view.cfg
        self.organize_channel = discord.ui.TextInput(
            label="一键整理·当前频道（默认 1）", default=str(cfg["organize_channel"]),
            min_length=1, max_length=2,
        )
        self.organize_category = discord.ui.TextInput(
            label="一键整理·当前子区（默认 2）", default=str(cfg["organize_category"]),
            min_length=1, max_length=2,
        )
        self.organize_guild = discord.ui.TextInput(
            label="一键整理·整个服务器（默认 3）", default=str(cfg["organize_guild"]),
            min_length=1, max_length=2,
        )
        self.reset = discord.ui.TextInput(
            label="初始化服务器（默认 3）", default=str(cfg["reset"]),
            min_length=1, max_length=2,
        )
        self.appeal = discord.ui.TextInput(
            label="申诉工单·解封/驳回各需（默认 2）", default=str(cfg["appeal"]),
            min_length=1, max_length=2,
        )
        for item in (
            self.organize_channel, self.organize_category,
            self.organize_guild, self.reset, self.appeal,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            values = {
                "organize_channel": int(self.organize_channel.value),
                "organize_category": int(self.organize_category.value),
                "organize_guild": int(self.organize_guild.value),
                "reset": int(self.reset.value),
                "appeal": int(self.appeal.value),
            }
            if any(v < 1 or v > 20 for v in values.values()):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ 参数格式不正确：票数需为 1~20 的整数。", ephemeral=True
            )
            return
        await self.view.cog.bot.db.set_vote_config(interaction.guild.id, **values)
        self.view.cfg = await self.view.cog.bot.db.get_vote_config(interaction.guild.id)
        await interaction.response.edit_message(
            embed=self.view.make_embed(), view=self.view
        )
        await self.view.cog.bot.log_admin(
            interaction.guild, interaction.user,
            "🗳️ 修改投票人数："
            f"整理·频道 {values['organize_channel']} 票 · "
            f"整理·子区 {values['organize_category']} 票 · "
            f"整理·全服 {values['organize_guild']} 票 · "
            f"初始化 {values['reset']} 票 · 申诉 {values['appeal']} 票",
        )


class VoteConfigView(discord.ui.View):
    """投票人数配置面板（/vote_config）。"""

    def __init__(self, cog: "AdminCog", cfg: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.cfg = dict(cfg)

    def make_embed(self) -> discord.Embed:
        cfg = self.cfg
        return discord.Embed(
            title="🗳️ 管理员投票人数配置",
            description=(
                "以下投票均由管理员点击按钮计数，**发起者自动计 1 票**，\n"
                "集齐所需人数即通过（范围 1~20，仅本服务器生效）：\n\n"
                f"🗂️ 一键整理·当前频道：**{cfg['organize_channel']}** 票\n"
                f"🗂️ 一键整理·当前子区：**{cfg['organize_category']}** 票\n"
                f"🗂️ 一键整理·整个服务器：**{cfg['organize_guild']}** 票\n"
                f"⚠️ 初始化服务器 /reset_server：**{cfg['reset']}** 票\n"
                f"🎫 申诉工单解封/驳回：各 **{cfg['appeal']}** 票\n\n"
                "点击「修改票数」按钮进行调整。"
            ),
            color=discord.Color.blurple(),
        )

    @discord.ui.button(label="修改票数", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit_votes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VoteConfigModal(self))


class ResetVoteView(discord.ui.View):
    """初始化投票：集齐 3 名管理员同意后执行服务器初始化。"""

    def __init__(
        self,
        cog: "AdminCog",
        guild: discord.Guild,
        initiator: discord.Member,
        delete_files: bool,
        required: int = DEFAULT_RESET_VOTES_REQUIRED,
    ):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.initiator = initiator
        self.delete_files = delete_files
        self.required = required
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
        self.cog._organize_cancel.discard(self.guild.id)  # 清掉可能残留的取消标记
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
            self.cog._organize_cancel.discard(self.guild.id)


class AdminCog(commands.Cog, name="管理"):
    def __init__(self, bot: RepoBot):
        self.bot = bot
        self._organizing: set[int] = set()
        # 已收到 /organize_cancel 取消请求的服务器（run_organize 在循环中检查）
        self._organize_cancel: set[int] = set()

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
            app_commands.Choice(name="当前频道", value="channel"),
            app_commands.Choice(name="当前子区", value="category"),
            app_commands.Choice(name="整个服务器", value="guild"),
        ]
    )
    @admin_only
    async def organize(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        assert interaction.guild is not None and isinstance(interaction.user, discord.Member)
        guild = interaction.guild
        if guild.id in self._organizing:
            await interaction.response.send_message(
                "❌ 当前已有整理任务在执行，请等待其完成。\n"
                "如确认任务卡死，可用 `/organize_cancel` 强制取消。", ephemeral=True
            )
            return
        vote_cfg = await self.bot.db.get_vote_config(guild.id)

        if scope.value == "channel":
            ch = interaction.channel
            if not isinstance(ch, discord.TextChannel):
                await interaction.response.send_message(
                    "❌ 请在目标文字频道中使用「当前频道」范围。", ephemeral=True
                )
                return
            settings = await self.bot.db.get_settings(guild.id)
            channel_ids = {ch.id}
            if settings and settings["storage_mode"] == "guild" and settings["storage_guild_id"]:
                # 独立存储服务器模式：已登记文件在存储服务器，按同名频道匹配迁移范围
                storage_guild = self.bot.get_guild(settings["storage_guild_id"])
                target = (
                    discord.utils.get(storage_guild.text_channels, name=ch.name)
                    if storage_guild
                    else None
                )
                channel_ids = {target.id} if target else set()
            scan_channel_ids = {ch.id}
            required = vote_cfg["organize_channel"]
            scope_label = f"当前频道 #{ch.name}"
        elif scope.value == "category":
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
            required = vote_cfg["organize_category"]
            scope_label = f"当前子区「{business_cat.name}」"
        else:
            channel_ids = None
            scan_channel_ids = None
            required = vote_cfg["organize_guild"]
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
            filter_mode=filter_mode, filter_ids=filter_ids,
        )
        if len(view.voters) >= view.required:
            # 所需票数为 1：发起者自动计票即达标，直接进入范围确认
            view.stop()
            confirm = OrganizeConfirmView(
                self, guild, scope_label, channel_ids, scan_channel_ids,
                sweep_plan, misplaced, interaction.user,
                filter_mode=filter_mode, filter_channels=filter_ids,
            )
            await progress.edit(content=None, embed=confirm.make_embed(), view=confirm)
            confirm.message = progress
            await self.bot.log_admin(
                guild, interaction.user,
                f"🗳️ 整理投票（{scope_label}）所需 1 票，发起者自动通过，等待确认范围",
            )
            return
        await progress.edit(content=None, embed=view.make_embed("🗂️ 整理投票"), view=view)
        view.message = progress
        await self.bot.log_admin(
            interaction.guild, interaction.user, f"🗳️ 发起整理投票（{scope_label}）"
        )

    @app_commands.command(
        name="organize_cancel",
        description="强制取消正在执行的整理/初始化任务（管理员）",
    )
    @admin_only
    async def organize_cancel(self, interaction: discord.Interaction):
        assert interaction.guild is not None
        gid = interaction.guild.id
        if gid not in self._organizing:
            await interaction.response.send_message(
                "✅ 当前没有执行中的整理/初始化任务。", ephemeral=True
            )
            return
        self._organize_cancel.add(gid)
        await interaction.response.send_message(
            "⏹️ 已请求取消：任务会在当前文件处理完后停止（已处理的部分不回滚）。",
            ephemeral=True,
        )
        await self.bot.log_admin(
            interaction.guild, interaction.user, "⏹️ 请求取消整理/初始化任务"
        )

    @app_commands.command(
        name="risk_config",
        description="配置下载风控：频率上限、处置方式（自动封禁/通知管理员）与封禁时长（管理员）",
    )
    @admin_only
    async def risk_config(self, interaction: discord.Interaction):
        assert interaction.guild is not None
        cfg = await self.bot.db.get_risk_config(interaction.guild.id)
        view = RiskConfigView(self, cfg)
        await interaction.response.send_message(
            embed=view.make_embed(), view=view, ephemeral=True
        )

    @app_commands.command(
        name="vote_config",
        description="配置各类投票所需的管理员同意人数：整理 / 初始化 / 申诉（管理员）",
    )
    @admin_only
    async def vote_config(self, interaction: discord.Interaction):
        assert interaction.guild is not None
        cfg = await self.bot.db.get_vote_config(interaction.guild.id)
        view = VoteConfigView(self, cfg)
        await interaction.response.send_message(
            embed=view.make_embed(), view=view, ephemeral=True
        )

    @app_commands.command(
        name="ticket_channel",
        description="设置风控申诉工单发送到的频道（管理员）",
    )
    @app_commands.describe(channel="接收申诉工单的文字频道，Bot 会在此 @管理员角色")
    @admin_only
    async def ticket_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        assert interaction.guild is not None
        await self.bot.db.upsert_settings(
            interaction.guild.id, ticket_channel_id=channel.id
        )
        await interaction.response.send_message(
            f"✅ 申诉工单频道已设置为 {channel.mention}。", ephemeral=True
        )
        await self.bot.log_admin(
            interaction.guild, interaction.user, f"🎫 设置工单频道：#{channel.name}"
        )

    # ───────────────────── 手动禁用 / 解除禁用 ─────────────────────

    @app_commands.command(
        name="ban_user", description="直接禁用成员使用 Bot 的全部指令（管理员）"
    )
    @app_commands.describe(
        member="要禁用的成员",
        hours="禁用时长（小时，可填 0.5）；不填或填 0 表示永久禁用",
        reason="禁用原因（会私信告知该成员）",
    )
    @admin_only
    async def ban_user(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        hours: float | None = None,
        reason: str | None = None,
    ):
        assert interaction.guild is not None
        if member.id == interaction.user.id:
            await interaction.response.send_message("❌ 不能禁用你自己。", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("❌ 不能禁用 Bot 账号。", ephemeral=True)
            return
        existing = await self.bot.db.get_active_ban(interaction.guild.id, member.id)
        if existing is not None:
            await interaction.response.send_message(
                f"⚠️ {member.mention} 已在禁用中（原因：{existing['reason']}），"
                "如需调整请先 `/unban_user` 解除后再重新禁用。",
                ephemeral=True,
            )
            return

        reason_text = (reason or "").strip() or "管理员手动禁用"
        permanent = hours is None or hours <= 0
        ban_hours = PERMANENT_BAN_HOURS if permanent else float(hours)
        until = await self.bot.db.ban_user(
            interaction.guild.id, member.id, ban_hours, reason_text
        )
        duration = "永久" if permanent else fmt_duration(ban_hours * 3600)

        # 该成员若有待处理风控工单，一并关闭，避免超时扫描重复封禁覆盖本次设置
        ticket_note = ""
        pending = await self.bot.db.get_pending_risk_review(
            interaction.guild.id, member.id
        )
        if pending is not None and await self.bot.db.close_risk_review(
            pending["id"], "banned"
        ):
            ticket_note = "\n🎫 该成员的待处理风控工单已一并关闭。"
            ch = (
                interaction.guild.get_channel(pending["channel_id"])
                if pending["channel_id"]
                else None
            )
            if isinstance(ch, discord.TextChannel) and pending["message_id"]:
                try:
                    msg = await ch.fetch_message(pending["message_id"])
                    embed = (
                        msg.embeds[0]
                        if msg.embeds
                        else discord.Embed(title="🚨 风控异常待处理")
                    )
                    embed.color = discord.Color.red()
                    embed.add_field(
                        name="处理结果",
                        value=(
                            f"⛔ 已由 {interaction.user.mention} 通过 `/ban_user` "
                            "手动禁用，本工单关闭"
                        ),
                        inline=False,
                    )
                    await msg.edit(embed=embed, view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        dm_ok = await self.bot.dm_user(
            member.id,
            f"⛔ 你已被服务器「{interaction.guild.name}」的管理员禁用文件仓库 Bot，"
            f"期限：**{duration}**"
            + ("" if permanent else f"（<t:{int(until)}:R> 解除）")
            + f"。\n原因：{reason_text}\n如有异议可到服务器内使用 `/appeal` 提交申诉。",
        )
        await self.bot.log_admin(
            interaction.guild,
            interaction.user,
            f"🚫 手动禁用成员：{member} (`{member.id}`)，期限 {duration}，"
            f"原因：{reason_text}"
            + ("（同时关闭其待处理风控工单）" if ticket_note else ""),
        )
        note = ticket_note
        if member.guild_permissions.administrator:
            note += "\n⚠️ 该成员拥有管理员权限，禁用不会限制其指令使用。"
        if not dm_ok:
            note += "\n📭 私信未送达（对方可能关闭了私信）。"
        await interaction.response.send_message(
            f"🚫 已禁用 {member.mention} 使用 Bot，期限：**{duration}**"
            + ("" if permanent else f"（<t:{int(until)}:R> 解除）")
            + f"\n原因：{reason_text}{note}",
            ephemeral=True,
        )

    @app_commands.command(
        name="unban_user", description="解除成员的 Bot 使用禁用（管理员）"
    )
    @app_commands.describe(member="要解除禁用的成员")
    @admin_only
    async def unban_user(self, interaction: discord.Interaction, member: discord.Member):
        assert interaction.guild is not None
        existing = await self.bot.db.get_active_ban(interaction.guild.id, member.id)
        if existing is None:
            await interaction.response.send_message(
                f"ℹ️ {member.mention} 当前没有生效中的禁用。", ephemeral=True
            )
            return
        await self.bot.db.unban_user(interaction.guild.id, member.id)
        dm_ok = await self.bot.dm_user(
            member.id,
            f"✅ 你在服务器「{interaction.guild.name}」的 Bot 使用禁用已被管理员解除，"
            "现在可以正常使用指令了。",
        )
        await self.bot.log_admin(
            interaction.guild,
            interaction.user,
            f"✅ 手动解除禁用：{member} (`{member.id}`)（原原因：{existing['reason']}）",
        )
        note = "" if dm_ok else "\n📭 私信未送达（对方可能关闭了私信）。"
        await interaction.response.send_message(
            f"✅ 已解除 {member.mention} 的 Bot 使用禁用。{note}", ephemeral=True
        )

    async def run_organize(
        self,
        guild: discord.Guild,
        channel_ids: set[int] | None,
        scan_channel_ids: set[int] | None,
        message: discord.Message | None,
        exclude_channel_ids: set[int] | None = None,
        include_channel_ids: set[int] | None = None,
        sweep_loose: bool = True,
    ) -> dict:
        """整理存储文件，分两阶段：

        ① 迁移：已登记但不在当前存储频道的文件，搬运到存储频道并更新索引；
        ② 扫描：范围内频道里未登记的文件消息（成员直接发的），登记入库并
           搬入存储频道；本就在存储频道的单附件消息原地登记。
           sweep_loose=False 时跳过阶段 ②（只迁移已登记文件）。

        执行中可通过 /organize_cancel 取消（抛 OrganizeCancelled）；
        存储频道被删除会立即中止（抛 RuntimeError），避免无效空跑。

        exclude/include_channel_ids 为确认面板决定的频道名单（阶段 1 仅套用
        exclude）：显式传入（含空集）即以面板为准，不再加载持久名单；
        均未传入（旧调用路径）则套用持久黑白名单。
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
            if guild.id in self._organize_cancel:
                raise OrganizeCancelled("整理被 /organize_cancel 取消")
            if self.bot.get_channel(storage.id) is None:
                raise RuntimeError(
                    "存储频道在整理过程中被删除，整理已中止（已处理的部分不回滚）。"
                )
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
        if not sweep_loose:
            log.info("整理：散落文件登记已关闭，跳过阶段 2（guild=%s）", guild.id)
            return stats
        if scan_channel_ids is None:
            scan_channels: list[discord.TextChannel] = list(guild.text_channels)
        else:
            scan_channels = [
                ch
                for cid in scan_channel_ids
                if isinstance((ch := guild.get_channel(cid)), discord.TextChannel)
            ]

        # 范围过滤：确认面板显式指定（名单已预填持久配置）则以面板为准；
        # 否则（旧调用路径）套用持久黑白名单
        explicit = include_channel_ids is not None or exclude_channel_ids is not None
        if not explicit:
            filter_mode, filter_ids = await self.bot.db.get_organize_filter(guild.id)
            if filter_ids:
                if filter_mode == "white":
                    scan_channels = [c for c in scan_channels if c.id in filter_ids]
                else:
                    scan_channels = [c for c in scan_channels if c.id not in filter_ids]
        if include_channel_ids is not None:
            scan_channels = [c for c in scan_channels if c.id in include_channel_ids]
        if exclude_channel_ids:
            scan_channels = [c for c in scan_channels if c.id not in exclude_channel_ids]

        me = self.bot.user
        scanned = 0
        for ch in scan_channels:
            try:
                async for msg in ch.history(limit=None, oldest_first=True):
                    if guild.id in self._organize_cancel:
                        raise OrganizeCancelled("整理被 /organize_cancel 取消")
                    if self.bot.get_channel(storage.id) is None:
                        raise RuntimeError(
                            "存储频道在整理过程中被删除，整理已中止（已处理的部分不回滚）。"
                        )
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
        description="初始化本服务器：清空数据，Bot 恢复到刚加入时的状态（需管理员投票，人数见 /vote_config）",
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
                "❌ 当前已有整理/初始化任务在执行，请等待其完成。\n"
                "如确认任务卡死，可用 `/organize_cancel` 强制取消。", ephemeral=True
            )
            return
        vote_cfg = await self.bot.db.get_vote_config(interaction.guild.id)
        view = ResetVoteView(
            self, interaction.guild, interaction.user,
            delete_files=(mode.value == "full"), required=vote_cfg["reset"],
        )
        await interaction.response.send_message(
            embed=view.make_embed("⚠️ 初始化投票"), view=view
        )
        view.message = await interaction.original_response()
        if len(view.voters) >= view.required:
            # 所需票数为 1：发起者自动计票即达标，直接执行
            view._disable()
            await view.message.edit(
                embed=view.make_embed("⚠️ 投票通过，开始初始化…"), view=view
            )
            view.stop()
            asyncio.create_task(view._run())
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
