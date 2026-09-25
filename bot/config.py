"""Bot 配置加载模块。"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    token: str
    storage_guild_id: int | None
    database_path: str
    max_file_size_mb: int
    # 服务条款 / 隐私政策全文链接（可选，配置后同意提示中展示）
    tos_url: str = ""
    privacy_url: str = ""




def load_config() -> Config:
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "未设置 DISCORD_TOKEN。请复制 .env.example 为 .env 并填入 Bot Token。"
        )

    storage_guild_raw = os.getenv("STORAGE_GUILD_ID", "").strip()
    storage_guild_id = int(storage_guild_raw) if storage_guild_raw.isdigit() else None

    database_path = os.getenv("DATABASE_PATH", "data/repository.db")
    max_file_size_mb = _int_env("MAX_FILE_SIZE_MB", 24)

    return Config(
        token=token,
        storage_guild_id=storage_guild_id,
        database_path=database_path,
        max_file_size_mb=max_file_size_mb,
        tos_url=os.getenv("TOS_URL", "").strip(),
        privacy_url=os.getenv("PRIVACY_URL", "").strip(),
    )
