"""启动入口：python run.py

运行日志查看方式：
- 控制台：前台运行时直接输出在终端
- 文件：默认写入 logs/bot.log（自动轮转：单文件 2MB，保留 5 份），
  可用环境变量 LOG_DIR 修改目录
- 后台托管时：systemd 用 journalctl -u <服务名> -f；Docker 用 docker logs -f <容器名>
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from bot.bot import RepoBot
from bot.config import load_config

LOG_DIR = os.getenv("LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "bot.log")


def main() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    # 运行日志同时写入文件，便于事后排查（整理失败明细、异常堆栈等）
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    logging.basicConfig(level=logging.INFO, handlers=[console, file_handler])
    logging.getLogger("repo-bot").info(
        "运行日志文件：%s（另见 README「日志查看」一节）", os.path.abspath(LOG_FILE)
    )

    config = load_config()
    bot = RepoBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
