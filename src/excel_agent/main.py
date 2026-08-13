"""ExcelMind v2 唯一正式启动入口。"""

from __future__ import annotations

import argparse

from .config import load_config, set_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ExcelMind HTTP 服务")
    parser.add_argument("command", choices=["serve"], help="启动 HTTP 服务")
    parser.add_argument("--config", "-c", default="config.yaml", help="配置文件路径")
    parser.add_argument("--host", default=None, help="覆盖监听地址")
    parser.add_argument("--port", "-p", type=int, default=None, help="覆盖监听端口")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    set_config(config)

    from .api import run_server

    run_server()


if __name__ == "__main__":
    main()
