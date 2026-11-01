"""在第二份隔离副本模拟首次下载失败，供浏览器验证无 React 时的真实认证恢复。"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from webui_host_harness import network_guard, prepare, prepare_resources


async def main():
    """显式构造缺失或旧页面场景，网络替身第二次交付实际构建包。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--resource-mode", choices=("missing", "legacy"), default="missing")
    parser.add_argument("--port", type=int, default=18732)
    args = parser.parse_args()
    prepare()
    prepare_resources(args.resource_mode, args.package if args.resource_mode == "legacy" else None)
    sys.addaudithook(network_guard)
    from src.webui.webui_server import WebUIServer

    server = WebUIServer("127.0.0.1", args.port)
    attempts = 0

    async def download(destination):
        """第一次返回网络失败，用户手动重试才交付完整 ZIP。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("synthetic first download failure")
        shutil.copyfile(args.package, destination)

    server.frontend._download = download
    try:
        await server.start()
    finally:
        await server.shutdown()
        print(f"RECOVERY_DOWNLOAD_ATTEMPTS={attempts}")


if __name__ == "__main__":
    asyncio.run(main())
