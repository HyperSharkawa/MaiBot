"""仅用于独立源码副本的真实 WebUI 集成服务；不启动聊天或真实模型。"""

import argparse
import asyncio
import ipaddress
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def network_guard(event, args):
    """测试进程只允许回环连接，外联立即失败；不读取生产凭据。"""
    if event == "socket.connect" and isinstance(args[1], tuple):
        host = args[1][0]
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise PermissionError("WebUI 隔离验收禁止外联")


def prepare() -> None:
    """从源码模板生成虚构配置；标记文件防止误在真实工作区运行。"""
    if not (ROOT / ".webui-test-fixture").is_file():
        raise SystemExit("只允许在显式标记的独立测试副本运行")
    import tomlkit

    config = ROOT / "config"
    config.mkdir(exist_ok=True)
    bot = tomlkit.parse((ROOT / "template/bot_config_template.toml").read_text(encoding="utf-8"))
    bot["bot"]["qq_account"] = "90000001"
    bot["bot"]["nickname"] = "测试机器人"
    bot["bot"]["alias_names"] = []
    bot["bot"]["platforms"] = []
    bot["chat"]["wait_for_image_recognition"] = False
    bot["telemetry"]["enable"] = False
    bot["tool"]["enable_tool"] = False
    bot["webui"]["mode"] = "development"
    bot["webui"]["anti_crawler_mode"] = "false"
    (config / "bot_config.toml").write_text(tomlkit.dumps(bot), encoding="utf-8")
    models = tomlkit.parse((ROOT / "template/model_config_template.toml").read_text(encoding="utf-8"))
    models["api_providers"] = [
        {
            "name": "fixture",
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "synthetic-not-a-real-key",
            "client_type": "openai",
            "max_retry": 1,
            "timeout": 1,
            "retry_interval": 0,
        }
    ]
    models["models"] = [
        {"name": name, "model_identifier": name, "api_provider": "fixture", "price_in": 0, "price_out": 0}
        for name in ("模型甲", "模型乙", "模型丙")
    ]
    for task in models["model_task_config"].values():
        task["model_list"] = ["模型甲", "模型乙", "模型丙"]
        task["selection_strategy"] = "priority"
    (config / "model_config.toml").write_text(tomlkit.dumps(models), encoding="utf-8")
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data/webui.json").write_text(
        json.dumps({"access_token": "webui-synthetic-test-token", "first_setup_completed": True}), encoding="utf-8"
    )


def prepare_resources(mode: str, package: Path | None = None) -> None:
    """显式准备无资源、自带、旧页面或托管缓存，不借用工作区残留产物。"""
    if not (ROOT / ".webui-test-fixture").is_file():
        raise ValueError("资源准备仅允许在隔离副本运行")
    if mode not in {"missing", "bundled", "legacy", "cache"}:
        raise ValueError("未知资源测试场景")
    bundled = ROOT / "webui/dist"
    cache = ROOT / "data/webui"
    if bundled.exists() or cache.exists():
        raise ValueError("请使用没有 dist 和托管缓存的干净测试副本")
    if mode == "missing":
        if package is not None:
            raise ValueError("无资源场景不接受资源包")
        return
    if package is None:
        raise ValueError("此资源场景需要明确指定已校验的 ZIP")
    from src.webui.frontend_contract import MANIFEST, build_identity, extract_archive, json_bytes

    identity = build_identity(ROOT)[0]
    target = cache / "releases" / identity if mode == "cache" else bundled
    target.mkdir(parents=True)
    extract_archive(package, target, identity)
    if mode == "legacy":
        (target / MANIFEST).unlink()
    elif mode == "cache":
        (cache / "current.json").write_bytes(json_bytes({"current": identity, "previous": None}))


async def main() -> None:
    """启动真实后端、认证与配置路由；所有持久化留在测试副本内。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18731)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--resource-mode", choices=("missing", "bundled", "legacy", "cache"), default="missing")
    parser.add_argument("--package", type=Path)
    args = parser.parse_args()
    if not (ROOT / ".webui-test-fixture").is_file():
        raise SystemExit("缺少隔离副本标记")
    os.chdir(ROOT)
    if args.prepare:
        prepare()
        prepare_resources(args.resource_mode, args.package)
    sys.addaudithook(network_guard)
    from src.webui.webui_server import WebUIServer

    server = WebUIServer(host="127.0.0.1", port=args.port)
    try:
        await server.start()
    finally:
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
