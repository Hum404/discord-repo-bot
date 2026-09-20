"""SQLite 数据库访问层（aiosqlite）。

三张表：
- files     : 文件元数据 + 存储消息定位
- downloads : 每一次下载行为（溯源核心）
- settings  : 每个业务服务器的存储配置
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    file_id            TEXT PRIMARY KEY,
    origin_guild_id    INTEGER NOT NULL,
    name               TEXT NOT NULL,
    size               INTEGER NOT NULL,
    content_type       TEXT,
    description        TEXT DEFAULT '',
    uploader_id        INTEGER NOT NULL,
    uploader_name      TEXT NOT NULL,
    uploaded_at        INTEGER NOT NULL,
    storage_channel_id INTEGER NOT NULL,
    storage_message_id INTEGER NOT NULL,
    download_count     INTEGER NOT NULL DEFAULT 0,
    seq                INTEGER
);

CREATE TABLE IF NOT EXISTS downloads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       TEXT NOT NULL,
    user_id       INTEGER NOT NULL,
    user_name     TEXT NOT NULL,
    guild_id      INTEGER,
    downloaded_at INTEGER NOT NULL,
    FOREIGN KEY (file_id) REFERENCES files (file_id)
);
CREATE INDEX IF NOT EXISTS idx_downloads_file ON downloads (file_id);
CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads (user_id);

CREATE TABLE IF NOT EXISTS settings (
    guild_id             INTEGER PRIMARY KEY,
    storage_mode         TEXT NOT NULL DEFAULT 'category',  -- 'category' | 'guild'
    storage_guild_id     INTEGER,
    storage_category_id  INTEGER,
    storage_channel_id   INTEGER,
    log_channel_id       INTEGER
);
"""


def new_file_id() -> str:
    return uuid.uuid4().hex[:8]


class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        # 旧库升级：补充 seq 列并回填编号
        try:
            await self._conn.execute("ALTER TABLE files ADD COLUMN seq INTEGER")
            await self._conn.commit()
        except Exception:
            pass  # 列已存在
        await self._backfill_seq()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database.connect() 未被调用"
        return self._conn

    async def _backfill_seq(self) -> None:
        """为旧文件按上传时间补发服务器内编号。"""
        cur = await self.conn.execute(
            "SELECT file_id, origin_guild_id FROM files WHERE seq IS NULL "
            "ORDER BY origin_guild_id, uploaded_at, file_id"
        )
        rows = await cur.fetchall()
        if not rows:
            return
        counters: dict[int, int] = {}
        for row in rows:
            gid = row["origin_guild_id"]
            if gid not in counters:
                cur2 = await self.conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM files WHERE origin_guild_id = ? AND seq IS NOT NULL",
                    (gid,),
                )
                (mx,) = await cur2.fetchone()
                counters[gid] = mx or 0
            counters[gid] += 1
            await self.conn.execute(
                "UPDATE files SET seq = ? WHERE file_id = ?", (counters[gid], row["file_id"])
            )
        await self.conn.commit()

    # ────────────────────────── settings ──────────────────────────

    async def get_settings(self, guild_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM settings WHERE guild_id = ?", (guild_id,)
        )
        return await cur.fetchone()

    async def upsert_settings(self, guild_id: int, **fields) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO settings (guild_id) VALUES (?)", (guild_id,)
        )
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            await self.conn.execute(
                f"UPDATE settings SET {cols} WHERE guild_id = ?",
                (*fields.values(), guild_id),
            )
        await self.conn.commit()

    # ────────────────────────── files ──────────────────────────

    async def add_file(
        self,
        *,
        origin_guild_id: int,
        name: str,
        size: int,
        content_type: str | None,
        description: str,
        uploader_id: int,
        uploader_name: str,
        storage_channel_id: int,
        storage_message_id: int,
    ) -> tuple[str, int]:
        file_id = new_file_id()
        async with self._write_lock:
            cur = await self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM files WHERE origin_guild_id = ?",
                (origin_guild_id,),
            )
            (seq,) = await cur.fetchone()
            await self.conn.execute(
                """
                INSERT INTO files (
                    file_id, origin_guild_id, name, size, content_type, description,
                    uploader_id, uploader_name, uploaded_at,
                    storage_channel_id, storage_message_id, seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    origin_guild_id,
                    name,
                    size,
                    content_type,
                    description,
                    uploader_id,
                    uploader_name,
                    int(time.time()),
                    storage_channel_id,
                    storage_message_id,
                    seq,
                ),
            )
            await self.conn.commit()
        return file_id, seq

    async def get_file(self, file_id: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM files WHERE file_id = ?", (file_id,)
        )
        return await cur.fetchone()

    async def get_file_by_seq(self, guild_id: int, seq: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM files WHERE origin_guild_id = ? AND seq = ?", (guild_id, seq)
        )
        return await cur.fetchone()

    async def list_files(
        self, guild_id: int, *, limit: int = 10, offset: int = 0
    ) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """
            SELECT * FROM files WHERE origin_guild_id = ?
            ORDER BY uploaded_at DESC LIMIT ? OFFSET ?
            """,
            (guild_id, limit, offset),
        )
        return await cur.fetchall()

    async def count_files(self, guild_id: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE origin_guild_id = ?", (guild_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def search_files(
        self, guild_id: int, keyword: str, *, limit: int = 10
    ) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """
            SELECT * FROM files
            WHERE origin_guild_id = ? AND (name LIKE ? OR description LIKE ?)
            ORDER BY uploaded_at DESC LIMIT ?
            """,
            (guild_id, f"%{keyword}%", f"%{keyword}%", limit),
        )
        return await cur.fetchall()

    async def delete_file(self, file_id: str) -> None:
        await self.conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
        await self.conn.execute("DELETE FROM downloads WHERE file_id = ?", (file_id,))
        await self.conn.commit()

    async def list_all_files(self, guild_id: int) -> list[aiosqlite.Row]:
        """不限量取出本服务器全部文件（供整理使用）。"""
        cur = await self.conn.execute(
            "SELECT * FROM files WHERE origin_guild_id = ? ORDER BY uploaded_at ASC",
            (guild_id,),
        )
        return await cur.fetchall()

    async def update_file_storage(
        self, file_id: str, channel_id: int, message_id: int
    ) -> None:
        await self.conn.execute(
            "UPDATE files SET storage_channel_id = ?, storage_message_id = ? WHERE file_id = ?",
            (channel_id, message_id, file_id),
        )
        await self.conn.commit()

    # ────────────────────────── downloads（溯源） ──────────────────────────

    async def log_download(
        self, *, file_id: str, user_id: int, user_name: str, guild_id: int | None
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO downloads (file_id, user_id, user_name, guild_id, downloaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (file_id, user_id, user_name, guild_id, int(time.time())),
        )
        await self.conn.execute(
            "UPDATE files SET download_count = download_count + 1 WHERE file_id = ?",
            (file_id,),
        )
        await self.conn.commit()

    async def get_download_history(
        self, file_id: str, *, limit: int = 20
    ) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """
            SELECT * FROM downloads WHERE file_id = ?
            ORDER BY downloaded_at DESC LIMIT ?
            """,
            (file_id, limit),
        )
        return await cur.fetchall()

    async def get_user_downloads(
        self, guild_id: int, user_id: int, *, limit: int = 20
    ) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """
            SELECT d.*, f.name AS file_name FROM downloads d
            JOIN files f ON f.file_id = d.file_id
            WHERE d.guild_id = ? AND d.user_id = ?
            ORDER BY d.downloaded_at DESC LIMIT ?
            """,
            (guild_id, user_id, limit),
        )
        return await cur.fetchall()
