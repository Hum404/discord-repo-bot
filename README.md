# Discord 文件仓库 Bot（Repo Bot）

一个以 **Discord 本身作为存储后端** 的文件仓库机器人：成员可以上传任意类型的文件，
所有文件统一归档到 **当前服务器的专用子区** 或 **独立存储服务器**（由管理员设置），
并且内置 **下载溯源机制** —— 每一次下载都会记录「谁、在什么时间、下载了哪个文件」。

## ✨ 功能特性

- 📤 **任意文件上传**：通过 `/upload` 上传任何附件，自动生成唯一文件 ID
- 🗂️ **两种存储模式**（管理员通过 `/storage_mode` 切换）：
  - **子区模式（category）**：自动在当前服务器创建 `📁 文件仓库` 分类 + 存储频道
  - **独立服务器模式（guild）**：将文件存储到另一个专门的 Discord 服务器
- 🕵️ **下载溯源**：下载必须经过 Bot 代理（`/download`），每次下载落库记录
  用户 ID、用户名、时间戳，并累计下载次数
- 📊 **审计能力**：管理员可查看任意文件的下载历史、任意成员的下载记录
- 🔍 **检索**：文件列表分页浏览、按名称/描述搜索
- 🗑️ **权限可控**：仅上传者或管理员可删除文件、查看下载历史
- 📝 **审计日志频道**：上传/下载事件自动推送到指定频道

## 📋 指令一览

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `/upload <附件> [描述]` | 上传文件，返回文件 ID | 所有人 |
| `/download <文件ID>` | 下载文件（**会被记录**） | 所有人 |
| `/files [页码]` | 浏览文件列表 | 所有人 |
| `/search <关键词>` | 搜索文件 | 所有人 |
| `/fileinfo <文件ID>` | 查看文件详情 | 所有人 |
| `/history <文件ID>` | 查看该文件的下载记录 | 上传者 / 管理员 |
| `/delete <文件ID>` | 删除文件 | 上传者 / 管理员 |
| `/storage_mode` | 设置存储模式（子区 / 独立服务器） | 管理员 |
| `/log_channel [频道]` | 设置审计日志频道 | 管理员 |
| `/audit_user <成员>` | 查看某成员的下载记录 | 管理员 |
| `/stats` | 仓库统计 | 管理员 |

## 🚀 部署

### 1. 创建 Discord Bot

1. 前往 [Discord Developer Portal](https://discord.com/developers/applications) 创建应用
2. 在 **Bot** 页面复制 Token
3. 在 **OAuth2 → URL Generator** 勾选 `bot` 和 `applications.commands`，
   Bot 权限至少勾选：`Manage Channels`、`Send Messages`、`Attach Files`、`Read Message History`
4. 用生成的链接邀请 Bot 进服务器（若使用独立存储服务器模式，也要邀请进存储服务器）

### 2. 配置

```bash
git clone https://github.com/Hum404/discord-repo-bot.git
cd discord-repo-bot
cp .env.example .env
# 编辑 .env，填入 DISCORD_TOKEN
```

`.env` 配置项：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `DISCORD_TOKEN` | ✅ | Bot Token |
| `STORAGE_GUILD_ID` | 独立服务器模式必填 | 存储服务器的 Guild ID |
| `DATABASE_PATH` | 否 | SQLite 数据库路径，默认 `data/repository.db` |
| `MAX_FILE_SIZE_MB` | 否 | 单文件大小上限（MB），默认 24 |

### 3. 运行

```bash
pip install -r requirements.txt
python run.py
```

Docker（可选）：

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "run.py"]
```

## 🔍 溯源机制说明

Discord 原生附件链接无法追踪访问者（且 CDN 链接会过期）。本 Bot 的做法是：

1. 上传时，文件被转存到 **Bot 控制的存储频道**，原始消息定位（频道 ID + 消息 ID）
   与元数据一起存入 SQLite
2. 下载时，用户必须通过 `/download <文件ID>` 向 Bot 索取，Bot 从存储频道取回文件并转发
3. 转发前先将 **文件 ID、用户 ID、用户名、服务器 ID、时间戳** 写入 `downloads` 表
4. 管理员/上传者可用 `/history` 和 `/audit_user` 随时审计；若配置了日志频道，
   每次上传/下载都会实时推送审计消息

> ⚠️ 注意：由于 Discord 平台限制，Bot 单文件发送上限受服务器加成等级影响
> （通常 25MB，加成等级 2 为 50MB，等级 3 为 100MB）。请按实际情况调整
> `MAX_FILE_SIZE_MB`。

## 🏗️ 项目结构

```
discord-repo-bot/
├── run.py               # 启动入口
├── requirements.txt
├── .env.example         # 配置模板
├── bot/
│   ├── bot.py           # Bot 主类、存储频道解析
│   ├── config.py        # 环境配置加载
│   ├── db.py            # SQLite 数据层（文件 / 下载记录 / 设置）
│   └── cogs/
│       ├── files.py     # 上传、下载、检索、溯源指令
│       └── admin.py     # 管理员配置与审计指令
└── LICENSE
```

## 📄 许可证

MIT License
