"""启动入口：python run.py"""
from __future__ import annotations

import logging

from bot.bot import RepoBot
from bot.config import load_config


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    config = load_config()
    bot = RepoBot(config)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
