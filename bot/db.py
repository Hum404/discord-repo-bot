"""SQLite 数据库访问层（aiosqlite）。

三张表：
- files     : 文件元数据 + 存储消息定位
- downloads : 每一次下载行为（审计核心）
- settings  : 每个业务服务器的存储配置

风控相关：
- bans            : 风控封禁（到期自动解除）
- download_events : 近期下载行为流水（用于频率判定）
- appeals         : 解封申诉工单
- risk_reviews    : 异常待处理工单（通知管理员模式，超时未处理自动封禁）
"""
from __future__ import annotations

import asyncio
import json
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
    password           TEXT,
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
    log_channel_id       INTEGER,
    admin_log_channel_id INTEGER,
    organize_mode        TEXT NOT NULL DEFAULT 'black',  -- 'black' | 'white'
    organize_channels    TEXT NOT NULL DEFAULT '[]',     -- JSON 数组：黑名单/白名单频道 ID
    risk_enabled         INTEGER NOT NULL DEFAULT 0,     -- 风控开关
    risk_window_minutes  INTEGER NOT NULL DEFAULT 10,    -- 风控统计窗口（分钟）
    risk_max_downloads   INTEGER NOT NULL DEFAULT 10,    -- 窗口内下载次数上限
    risk_ban_hours       REAL NOT NULL DEFAULT 1,        -- 触发后封禁时长（小时）
    risk_action_mode     TEXT NOT NULL DEFAULT 'auto',   -- 触发处置：'auto' 自动封禁 | 'review' 通知管理员
    risk_review_minutes  INTEGER NOT NULL DEFAULT 30,    -- 通知模式下管理员处理时限（分钟），超时自动封禁
    ticket_channel_id    INTEGER,                        -- 申诉工单发送频道
    vote_organize_category INTEGER NOT NULL DEFAULT 2,   -- /organize 当前子区所需管理员同意人数
    vote_organize_guild    INTEGER NOT NULL DEFAULT 3,   -- /organize 整个服务器所需管理员同意人数
    vote_reset             INTEGER NOT NULL DEFAULT 3,   -- /reset_server 所需管理员同意人数
    vote_appeal            INTEGER NOT NULL DEFAULT 2    -- /appeal 工单解封/驳回各需票数
);

CREATE TABLE IF NOT EXISTS bans (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    banned_until REAL NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS download_events (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dl_events_user ON download_events (guild_id, user_id, ts);

CREATE TABLE IF NOT EXISTS appeals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'approved' | 'rejected'
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_appeals_user ON appeals (guild_id, user_id, status);

CREATE TABLE IF NOT EXISTS risk_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    ban_hours  REAL NOT NULL DEFAULT 1,                -- 封禁时采用的时长（创建时快照）
    deadline   REAL NOT NULL,                          -- 管理员处理时限（到期自动封禁）
    status     TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'banned' | 'dismissed' | 'auto_banned'
    channel_id INTEGER,
    message_id INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risk_reviews_pending ON risk_reviews (status, deadline);
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
        # 旧库升级：逐列补充（列已存在则忽略），并回填编号
        for migration in (
            "ALTER TABLE files ADD COLUMN seq INTEGER",
            "ALTER TABLE files ADD COLUMN password TEXT",
            "ALTER TABLE settings ADD COLUMN admin_log_channel_id INTEGER",
            "ALTER TABLE settings ADD COLUMN organize_mode TEXT NOT NULL DEFAULT 'black'",
            "ALTER TABLE settings ADD COLUMN organize_channels TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE settings ADD COLUMN risk_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN risk_window_minutes INTEGER NOT NULL DEFAULT 10",
            "ALTER TABLE settings ADD COLUMN risk_max_downloads INTEGER NOT NULL DEFAULT 10",
            "ALTER TABLE settings ADD COLUMN risk_ban_hours REAL NOT NULL DEFAULT 1",
            "ALTER TABLE settings ADD COLUMN risk_action_mode TEXT NOT NULL DEFAULT 'auto'",
            "ALTER TABLE settings ADD COLUMN risk_review_minutes INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE settings ADD COLUMN ticket_channel_id INTEGER",
            "ALTER TABLE settings ADD COLUMN vote_organize_category INTEGER NOT NULL DEFAULT 2",
            "ALTER TABLE settings ADD COLUMN vote_organize_guild INTEGER NOT NULL DEFAULT 3",
            "ALTER TABLE settings ADD COLUMN vote_reset INTEGER NOT NULL DEFAULT 3",
            "ALTER TABLE settings ADD COLUMN vote_appeal INTEGER NOT NULL DEFAULT 2",
        ):
            try:
                await self._conn.execute(migration)
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

    async def get_organize_filter(self, guild_id: int) -> tuple[str, set[int]]:
        """一键整理的频道黑白名单。返回 (模式, 频道 ID 集合)。

        模式：black = 整理名单以外的所有频道（默认）；white = 只整理名单内的频道。
        """
        row = await self.get_settings(guild_id)
        if row is None:
            return "black", set()
        keys = row.keys()
        mode = row["organize_mode"] if "organize_mode" in keys else "black"
        if mode not in ("black", "white"):
            mode = "black"
        raw = row["organize_channels"] if "organize_channels" in keys else "[]"
        try:
            ids = {int(x) for x in json.loads(raw or "[]")}
        except (ValueError, TypeError):
            ids = set()
        return mode, ids

    async def set_organize_filter(
        self, guild_id: int, mode: str, channel_ids
    ) -> None:
        if mode not in ("black", "white"):
            mode = "black"
        await self.upsert_settings(
            guild_id,
            organize_mode=mode,
            organize_channels=json.dumps([int(x) for x in channel_ids]),
        )

    # ────────────────────────── 投票人数配置 ──────────────────────────

    # 各类管理员投票所需同意人数的默认值与取值范围（1~20）
    VOTE_DEFAULTS = {
        "organize_category": 2,  # /organize 当前子区
        "organize_guild": 3,     # /organize 整个服务器
        "reset": 3,              # /reset_server
        "appeal": 2,             # /appeal 工单解封/驳回各需
    }
    VOTE_MIN, VOTE_MAX = 1, 20

    async def get_vote_config(self, guild_id: int) -> dict:
        """各类投票所需的管理员同意人数（键见 VOTE_DEFAULTS）。"""
        row = await self.get_settings(guild_id)
        cfg = dict(self.VOTE_DEFAULTS)
        if row is not None:
            keys = row.keys()
            for name, default in self.VOTE_DEFAULTS.items():
                col = f"vote_{name}"
                if col in keys:
                    try:
                        value = int(row[col])
                    except (TypeError, ValueError):
                        continue
                    if self.VOTE_MIN <= value <= self.VOTE_MAX:
                        cfg[name] = value
        return cfg

    async def set_vote_config(self, guild_id: int, **fields) -> None:
        """更新投票人数配置；fields 形如 organize_category=3，自动钳制到 1~20。"""
        allowed = {}
        for name in self.VOTE_DEFAULTS:
            if name in fields:
                value = int(fields[name])
                allowed[f"vote_{name}"] = min(max(value, self.VOTE_MIN), self.VOTE_MAX)
        if allowed:
            await self.upsert_settings(guild_id, **allowed)

    # ────────────────────────── 风控（下载频率限制） ──────────────────────────

    async def get_risk_config(self, guild_id: int) -> dict:
        """风控配置：开关 / 统计窗口 / 次数上限 / 封禁时长。"""
        row = await self.get_settings(guild_id)
        cfg = {
            "enabled": False,
            "window_minutes": 10,
            "max_downloads": 10,
            "ban_hours": 1.0,
            "action_mode": "auto",  # 'auto' 立即封禁 | 'review' 通知管理员，超时自动封禁
            "review_minutes": 30,
        }
        if row is None:
            return cfg
        keys = row.keys()
        if "risk_enabled" in keys:
            cfg["enabled"] = bool(row["risk_enabled"])
        if "risk_window_minutes" in keys and row["risk_window_minutes"]:
            cfg["window_minutes"] = int(row["risk_window_minutes"])
        if "risk_max_downloads" in keys and row["risk_max_downloads"]:
            cfg["max_downloads"] = int(row["risk_max_downloads"])
        if "risk_ban_hours" in keys and row["risk_ban_hours"]:
            cfg["ban_hours"] = float(row["risk_ban_hours"])
        if "risk_action_mode" in keys and row["risk_action_mode"] in ("auto", "review"):
            cfg["action_mode"] = row["risk_action_mode"]
        if "risk_review_minutes" in keys and row["risk_review_minutes"]:
            cfg["review_minutes"] = int(row["risk_review_minutes"])
        return cfg

    async def set_risk_config(
        self,
        guild_id: int,
        *,
        enabled: bool | None = None,
        window_minutes: int | None = None,
        max_downloads: int | None = None,
        ban_hours: float | None = None,
        action_mode: str | None = None,
        review_minutes: int | None = None,
    ) -> None:
        fields: dict = {}
        if enabled is not None:
            fields["risk_enabled"] = 1 if enabled else 0
        if window_minutes is not None:
            fields["risk_window_minutes"] = max(1, int(window_minutes))
        if max_downloads is not None:
            fields["risk_max_downloads"] = max(1, int(max_downloads))
        if ban_hours is not None:
            fields["risk_ban_hours"] = max(0.05, float(ban_hours))
        if action_mode in ("auto", "review"):
            fields["risk_action_mode"] = action_mode
        if review_minutes is not None:
            fields["risk_review_minutes"] = max(1, int(review_minutes))
        if fields:
            await self.upsert_settings(guild_id, **fields)

    async def record_download_event(self, guild_id: int, user_id: int) -> None:
        """记录一次下载行为，并清理 24 小时前的流水。"""
        now = time.time()
        await self.conn.execute(
            "INSERT INTO download_events (guild_id, user_id, ts) VALUES (?, ?, ?)",
            (guild_id, user_id, now),
        )
        await self.conn.execute(
            "DELETE FROM download_events WHERE ts < ?", (now - 86400,)
        )
        await self.conn.commit()

    async def count_recent_downloads(
        self, guild_id: int, user_id: int, window_minutes: int
    ) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM download_events "
            "WHERE guild_id = ? AND user_id = ? AND ts >= ?",
            (guild_id, user_id, time.time() - window_minutes * 60),
        )
        (count,) = await cur.fetchone()
        return int(count)

    async def ban_user(
        self, guild_id: int, user_id: int, hours: float, reason: str
    ) -> float:
        """封禁成员，返回解封时间戳。"""
        until = time.time() + hours * 3600
        await self.conn.execute(
            "INSERT OR REPLACE INTO bans (guild_id, user_id, reason, banned_until, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, reason, until, time.time()),
        )
        await self.conn.commit()
        return until

    async def get_active_ban(self, guild_id: int, user_id: int):
        """返回生效中的封禁记录；过期则自动解除并返回 None。"""
        cur = await self.conn.execute(
            "SELECT * FROM bans WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        if row["banned_until"] <= time.time():
            await self.unban_user(guild_id, user_id)
            return None
        return row

    async def unban_user(self, guild_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM bans WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()

    # ────────────────────────── 申诉工单 ──────────────────────────

    async def create_appeal(
        self, guild_id: int, user_id: int, channel_id: int, message_id: int
    ) -> int:
        cur = await self.conn.execute(
            "INSERT INTO appeals (guild_id, user_id, channel_id, message_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, channel_id, message_id, time.time()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def get_open_appeal(self, guild_id: int, user_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM appeals WHERE guild_id = ? AND user_id = ? AND status = 'open' "
            "ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        )
        return await cur.fetchone()

    async def close_appeal(self, appeal_id: int, status: str) -> None:
        await self.conn.execute(
            "UPDATE appeals SET status = ? WHERE id = ?", (status, appeal_id)
        )
        await self.conn.commit()

    async def list_open_appeals(self) -> list:
        """全部未结案工单（启动时恢复投票按钮用）。"""
        cur = await self.conn.execute(
            "SELECT * FROM appeals WHERE status = 'open' ORDER BY id"
        )
        return await cur.fetchall()

    # ────────────────────────── 风控待处理工单 ──────────────────────────

    async def create_risk_review(
        self,
        guild_id: int,
        user_id: int,
        reason: str,
        ban_hours: float,
        deadline: float,
    ) -> int:
        """创建一条异常待处理工单，返回工单 ID。"""
        cur = await self.conn.execute(
            "INSERT INTO risk_reviews (guild_id, user_id, reason, ban_hours, deadline, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, user_id, reason, ban_hours, deadline, time.time()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def set_risk_review_message(
        self, review_id: int, channel_id: int, message_id: int
    ) -> None:
        """回写工单通知消息定位（重启后恢复按钮、超时后编辑消息用）。"""
        await self.conn.execute(
            "UPDATE risk_reviews SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel_id, message_id, review_id),
        )
        await self.conn.commit()

    async def get_pending_risk_review(self, guild_id: int, user_id: int):
        cur = await self.conn.execute(
            "SELECT * FROM risk_reviews WHERE guild_id = ? AND user_id = ? "
            "AND status = 'pending' ORDER BY id DESC LIMIT 1",
            (guild_id, user_id),
        )
        return await cur.fetchone()

    async def close_risk_review(self, review_id: int, status: str) -> bool:
        """条件更新：仅当工单仍处于 pending 时置为目标状态。

        返回是否更新成功——管理员按钮与后台超时扫描以此防止重复处置。
        """
        cur = await self.conn.execute(
            "UPDATE risk_reviews SET status = ? WHERE id = ? AND status = 'pending'",
            (status, review_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def list_pending_risk_reviews(self) -> list:
        """全部待处理工单（启动时恢复处理按钮用）。"""
        cur = await self.conn.execute(
            "SELECT * FROM risk_reviews WHERE status = 'pending' ORDER BY id"
        )
        return await cur.fetchall()

    async def list_expired_risk_reviews(self, now: float) -> list:
        """已超过处理时限仍未处理的工单（后台扫描自动封禁用）。"""
        cur = await self.conn.execute(
            "SELECT * FROM risk_reviews WHERE status = 'pending' AND deadline <= ?",
            (now,),
        )
        return await cur.fetchall()

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
        password: str | None = None,
        uploaded_at: int | None = None,
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
                    storage_channel_id, storage_message_id, password, seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    uploaded_at if uploaded_at is not None else int(time.time()),
                    storage_channel_id,
                    storage_message_id,
                    password,
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

    async def purge_guild(self, guild_id: int) -> dict:
        """初始化：清空某服务器的全部文件记录、下载记录与设置。返回统计。"""
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE origin_guild_id = ?", (guild_id,)
        )
        files_n = (await cur.fetchone())[0]
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE guild_id = ?", (guild_id,)
        )
        dl_n = (await cur.fetchone())[0]
        async with self._write_lock:
            await self.conn.execute(
                "DELETE FROM downloads WHERE guild_id = ? OR file_id IN "
                "(SELECT file_id FROM files WHERE origin_guild_id = ?)",
                (guild_id, guild_id),
            )
            await self.conn.execute(
                "DELETE FROM files WHERE origin_guild_id = ?", (guild_id,)
            )
            await self.conn.execute(
                "DELETE FROM settings WHERE guild_id = ?", (guild_id,)
            )
            await self.conn.commit()
        return {"files": files_n, "downloads": dl_n}

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

    # ────────────────────────── downloads（下载记录） ──────────────────────────

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
