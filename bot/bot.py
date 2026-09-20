"""Bot 主类：初始化、指令同步、存储频道解析。"""
from __future__ import annotations

import logging

import discord
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
        await self.load_extension("bot.cogs.files")
        await self.load_extension("bot.cogs.admin")

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("已登录：%s (ID: %s)", self.user, self.user.id)
        # 按服务器同步斜杠指令（立即生效，无需等待全局传播）
        for guild in self.guilds:
            try:
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                log.info("指令已同步到服务器：%s", guild.name)
            except discord.HTTPException as exc:
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


def fmt_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"
