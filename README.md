# Discord 文件仓库 Bot（Repo Bot）

一个以 **Discord 本身作为存储后端** 的文件仓库机器人：成员可以上传任意类型的文件，
所有文件统一归档到 **当前服务器的专用子区** 或 **独立存储服务器**（由管理员设置），
并且内置 **下载溯源机制** —— 每一次下载都会记录「谁、在什么时间、下载了哪个文件」。

## ✨ 功能特性

- 📤 **任意文件上传**：通过 `/upload` 上传任何附件，自动生成唯一文件 ID
- 🗂️ **两种存储模式**（管理员通过 `/storage_mode` 切换）：
  - **子区模式（category）**：自动在当前服务器创建 `📁 文件仓库` 分类 + 存储频道，
    也可以通过 `category` 参数**直接使用你已建好的子区**
  - **独立服务器模式（guild）**：将文件存储到另一个专门的 Discord 服务器
- 🔢 **文件编号**：每个文件自动分配服务器内编号（#1、#2……），
  `/download`、`/fileinfo`、`/history`、`/delete` 均可直接输入编号操作
- 🗳️ **一键整理**：`/organize` 发起管理员投票（整理当前子区需 2 名管理员同意，
  整个服务器需 3 名，发起者自动计 1 票），自动迁移旧位置文件，
  并**扫描范围内频道里未登记的散落文件**（成员直接发的），登记入库后归拢到存储频道
- 🔒 **下载密码**：上传时可选择设置密码（弹窗输入），下载该文件需输入正确密码，
  密码错误会记入管理日志
- ✏️ **上传重命名**：上传准备页可直接修改入库文件名（自动保留扩展名），
  改名记录同步到管理日志
- 🛡️ **管理日志**：`/storage_mode`、`/log_channel`、`/organize`、`/delete` 等
  管理员操作自动记录，可用 `/admin_log_channel` 单独设置管理日志频道
  （未设置时跟随审计日志频道）
- 🔖 **文件溯源码**：下载时在文件副本中注入「下载者 ID + 时间」标记
  （ZIP 系写入 comment、文本追加零宽字符、图片右下角水印），
  外泄文件可用 `/trace` 反查下载者
- 🕵️ **下载溯源**：下载必须经过 Bot 代理（`/download`），每次下载落库记录
  用户 ID、用户名、时间戳，并累计下载次数
- 📊 **审计能力**：管理员可查看任意文件的下载历史、任意成员的下载记录
- 🔍 **检索**：文件列表分页浏览、按名称/描述搜索
- 🗑️ **权限可控**：仅上传者或管理员可删除文件、查看下载历史
- 📝 **审计日志频道**：上传/下载事件自动推送到指定频道

## 📋 指令一览

| 指令 | 说明 | 权限 |
| --- | --- | --- |
| `/upload <附件> [描述]` | 上传文件（可设密码、重命名），返回文件 ID | 所有人 |
| `/download <文件ID>` | 下载文件（**会被记录**） | 所有人 |
| `/files [页码]` | 浏览文件列表 | 所有人 |
| `/search <关键词>` | 搜索文件 | 所有人 |
| `/fileinfo <文件ID>` | 查看文件详情 | 所有人 |
| `/history <文件ID>` | 查看该文件的下载记录 | 上传者 / 管理员 |
| `/delete <文件ID>` | 删除文件 | 上传者 / 管理员 |
| `/storage_mode` | 设置存储模式（子区 / 独立服务器） | 管理员 |
| `/log_channel [频道]` | 设置审计日志频道 | 管理员 |
| `/admin_log_channel [频道]` | 单独设置管理日志频道 | 管理员 |
| `/trace <附件>` | 读取外泄文件中的溯源标记，定位下载者 | 管理员 |
| `/audit_user <成员>` | 查看某成员的下载记录 | 管理员 |
| `/stats` | 仓库统计 | 管理员 |
| `/organize` | 投票整理存储文件（子区 2 票 / 全服 3 票） | 管理员 |

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

## 📝 更新日志

### v1.3.0

- ✨ **文件溯源码注入**：`/download` 发放的文件副本会自动注入「下载者 ID + 时间」标记——
  ZIP 系（zip/apk/docx/xlsx 等）写入 ZIP comment（不破坏 APK v2 签名）；
  文本类追加零宽字符编码（不可见）；图片右下角打半透明水印（需 Pillow）
- ✨ 新增 `/trace` 指令（管理员）：上传疑似外泄的文件，自动读取溯源标记并定位
  下载者与下载时间；图片水印为可见文字，按提示查看右下角
- ✨ 下载审计日志增加「溯源标记已注入」标识

### v1.2.0

- 🐛 修复/完善：`/organize` 整理不到文件的问题——原逻辑只迁移「已登记但不在
  存储频道」的文件，正常上传的文件本来就在存储频道，因此永远显示已归拢 0 个。
  现在新增**散落文件扫描**：范围内频道中未登记的文件消息会自动登记入库并归拢到
  存储频道（本就在存储频道的单附件消息原地登记，多附件消息拆分入库）
- ✨ 上传流程改为准备页：`/upload` 后可选择**设置下载密码**（弹窗输入）、
  **重命名文件**（自动保留扩展名），确认后再写入存储频道
- ✨ 下载密码保护：加密文件 `/download` 时弹窗验证密码，密码错误记入管理日志
- ✨ 新增管理日志：`/storage_mode`、`/log_channel`、`/admin_log_channel`、
  `/audit_user`、`/organize`、管理员删除文件、上传重命名等操作自动记录；
  新增 `/admin_log_channel` 指令单独设置管理日志频道（未设置时跟随审计日志频道）
- ✨ `/fileinfo` 增加密码保护标识

### v1.1.0

- 🐛 修复：`/storage_mode` 存储服务器 ID 增加格式校验与明确报错（ID 需为纯数字；
  Bot 未加入目标服务器时会明确提示，不再静默失败）
- ✨ `/storage_mode` 子区模式新增 `category` 参数：可直接使用已建好的子区，
  不填则保持原行为（自动新建专用子区）
- ✨ 新增 `/organize` 一键整理：管理员投票制（当前子区 2 票、整个服务器 3 票），
  自动迁移散落文件、保留原入库卡片并同步更新索引
- ✨ 文件自动编号：上传回执、`/files`、`/search`、`/fileinfo` 均显示编号，
  相关指令可直接用编号代替文件 ID
- 🔧 移除不必要的 Privileged Intents 申请（避免未开启时启动报错）

## 📄 许可证

MIT License
