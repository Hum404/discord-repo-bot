"""Bot 主类：初始化、指令同步、存储频道解析。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .db import Database

log = logging.getLogger("repo-bot")

STORAGE_CATEGORY_NAME = "📁 文件仓库"
STORAGE_CHANNEL_NAME = "repository-storage"


class RepoBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.db = Database(config.database_path)

    async def setup_hook(self) -> None:
        await self.db.connect()
        self.tree.on_error = self._on_app_command_error
        await self.load_extension("bot.cogs.files")
        await self.load_extension("bot.cogs.admin")

    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """全局指令错误处理：避免异常时用户只能看到「应用程序未响应」。"""
        if isinstance(error, app_commands.MissingPermissions):
            text = "❌ 需要管理员权限才能使用该指令。"
        elif isinstance(error, app_commands.NoPrivateMessage):
            text = "❌ 该指令只能在服务器中使用。"
        elif isinstance(error, app_commands.CheckFailure):
            if interaction.response.is_done():
                return  # 检查函数已自行回复（如风控封禁提示）
            text = "❌ 你无法使用该指令。"
        else:
            log.exception(
                "指令 /%s 执行失败",
                interaction.command.name if interaction.command else "?",
                exc_info=error,
            )
            text = "❌ 指令执行出错，请重试；持续失败请联系管理员查看 Bot 运行日志（logs/bot.log）。"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("已登录：%s (ID: %s)", self.user, self.user.id)
        # 按服务器同步斜杠指令（立即生效，无需等待全局传播）
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("指令已同步到服务器：%s", guild.name)
            except Exception as exc:
                log.warning("同步指令到 %s 失败：%s", guild.name, exc)

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    # ───────────────────── 存储频道解析 ─────────────────────

    async def resolve_storage_channel(
        self, guild: discord.Guild
    ) -> tuple[discord.TextChannel | None, str | None]:
        """根据管理员设置解析存储频道。

        返回 (频道, 错误信息)。出错时频道为 None。
        """
        settings = await self.db.get_settings(guild.id)
        mode = settings["storage_mode"] if settings else "category"

        # 已配置好的存储频道直接使用
        if settings and settings["storage_channel_id"]:
            channel = self.get_channel(settings["storage_channel_id"])
            if isinstance(channel, discord.TextChannel):
                return channel, None
            # 频道被删除，重置后继续走自动创建流程
            await self.db.upsert_settings(guild.id, storage_channel_id=None)

        if mode == "guild":
            return await self._resolve_guild_storage(guild, settings)
        return await self._resolve_category_storage(guild, settings)

    async def _resolve_category_storage(
        self, guild: discord.Guild, settings
    ) -> tuple[discord.TextChannel | None, str | None]:
        """在当前服务器创建/复用存储子区（分类 + 频道）。"""
        category = None
        if settings and settings["storage_category_id"]:
            category = guild.get_channel(settings["storage_category_id"])
        if category is None:
            category = discord.utils.get(guild.categories, name=STORAGE_CATEGORY_NAME)
        if category is None:
            try:
                category = await guild.create_category(STORAGE_CATEGORY_NAME)
            except discord.Forbidden:
                return None, "缺少权限：无法创建存储分类（需要「管理频道」权限）。"

        channel = discord.utils.get(category.text_channels, name=STORAGE_CHANNEL_NAME)
        if channel is None:
            try:
                channel = await category.create_text_channel(STORAGE_CHANNEL_NAME)
            except discord.Forbidden:
                return None, "缺少权限：无法在存储分类下创建频道。"

        await self.db.upsert_settings(
            guild.id,
            storage_mode="category",
            storage_category_id=category.id,
            storage_channel_id=channel.id,
        )
        return channel, None

    async def _resolve_guild_storage(
        self, guild: discord.Guild, settings
    ) -> tuple[discord.TextChannel | None, str | None]:
        """使用独立存储服务器中的频道。"""
        storage_guild_id = (
            (settings["storage_guild_id"] if settings else None)
            or self.config.storage_guild_id
        )
        if not storage_guild_id:
            return None, (
                "存储模式为「独立服务器」，但未配置存储服务器 ID。\n"
                "请使用 `/storage_mode` 重新设置，或在 .env 中配置 STORAGE_GUILD_ID。"
            )
        storage_guild = self.get_guild(storage_guild_id)
        if storage_guild is None:
            return None, (
                f"找不到存储服务器 (ID: {storage_guild_id})。\n"
                "请确认 Bot 已被邀请进该服务器。"
            )

        channel = discord.utils.get(storage_guild.text_channels, name=STORAGE_CHANNEL_NAME)
        if channel is None:
            try:
                channel = await storage_guild.create_text_channel(STORAGE_CHANNEL_NAME)
            except discord.Forbidden:
                return None, "Bot 在存储服务器中缺少「管理频道」权限，无法创建存储频道。"

        await self.db.upsert_settings(
            guild.id, storage_guild_id=storage_guild_id, storage_channel_id=channel.id
        )
        return channel, None

    async def send_log(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """向日志频道发送审计消息（如果已配置）。"""
        settings = await self.db.get_settings(guild.id)
        if not settings or not settings["log_channel_id"]:
            return
        channel = guild.get_channel(settings["log_channel_id"])
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                pass

    async def dm_user(self, user_id: int, content: str) -> bool:
        """私信通知用户。对方关闭私信等情况失败时返回 False，不抛异常。"""
        try:
            user = await self.fetch_user(user_id)
            await user.send(content)
            return True
        except discord.HTTPException:
            return False
        except Exception:
            log.warning("私信用户 %s 失败", user_id, exc_info=True)
            return False

    async def log_admin(
        self,
        guild: discord.Guild,
        actor: discord.User | discord.Member,
        action: str,
    ) -> None:
        """记录管理员操作到管理日志频道。

        未单独设置管理日志频道时，跟随审计日志频道；两者都未设置则丢弃。
        """
        settings = await self.db.get_settings(guild.id)
        if not settings:
            return
        channel_id = settings["admin_log_channel_id"] or settings["log_channel_id"]
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(
            title="🛡️ 管理日志",
            description=action,
            color=discord.Color.dark_gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=f"{actor} ({actor.id})", icon_url=actor.display_avatar.url
        )
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass


def fmt_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
