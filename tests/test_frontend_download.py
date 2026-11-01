"""真实回环 HTTP 验证换源、进度、低速退出和选源认证。"""

import asyncio
import hashlib
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from aiohttp import web
from fastapi import FastAPI, Header, HTTPException

from src.webui.frontend_assets import FrontendAssets
from src.webui.frontend_contract import package_directory
from src.webui.frontend_routes import register_frontend_routes
from src.webui.frontend_sources import DownloadSource
from test_frontend_contract import small_package, source_copy


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    """下载地址映射到本例服务，保持实际流式读取、落盘、校验和激活。"""

    async def asyncSetUp(self):
        """建立独立包与服务器，退出时先关下载任务再清理磁盘。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.root = source_copy(self.work / "source")
        self.package = self.work / "package"
        self.identity = small_package(self.root, self.package)
        self.modes = {}
        self.requests = []
        self.opened = asyncio.Event()
        self.release = asyncio.Event()
        app = web.Application()
        app.router.add_get("/{source}/{name}", self.serve)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        self.addAsyncCleanup(self.release_waiters)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        port = self.port

        def local_url(source, identity):
            """只在测试中改写固定发行目录，不向产品增加自定义 URL 入口。"""
            return f"http://127.0.0.1:{port}/{source.id}"

        self.url_patch = patch.object(DownloadSource, "base_url", local_url)
        self.url_patch.start()
        self.addCleanup(self.url_patch.stop)
        self.assets = FrontendAssets(self.root)
        self.addAsyncCleanup(self.assets.close)

    async def release_waiters(self):
        """解除测试服务器等待，不遗留受控响应。"""
        self.release.set()

    async def serve(self, request):
        """按源模拟 HTTP、内容和流量失败；所有响应仅在回环发送。"""
        source, name = request.match_info["source"], request.match_info["name"]
        self.requests.append((source, name))
        mode = self.modes.get(source, "valid")
        if mode == "404":
            raise web.HTTPNotFound()
        if mode == "403":
            raise web.HTTPForbidden()
        content = (self.package / name).read_bytes()
        if mode == "bad-check" and name.endswith("sha256"):
            return web.Response(text="0" * 64 + "  webui.zip\n")
        if mode == "html":
            content = (
                b"<html>proxy error</html>"
                if name.endswith("zip")
                else (hashlib.sha256(b"<html>proxy error</html>").hexdigest() + "  webui.zip\n").encode()
            )
        if mode in {"stall", "drip", "unknown", "slow-valid", "truncated"} and name.endswith("zip"):
            headers = {} if mode in {"unknown", "drip"} else {"Content-Length": str(len(content))}
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            self.opened.set()
            try:
                if mode == "stall":
                    await self.release.wait()
                elif mode == "drip":
                    while not self.release.is_set():
                        await response.write(b"x")
                        await asyncio.sleep(0.02)
                elif mode == "truncated":
                    await response.write(content[:10])
                    request.transport.close()
                elif mode == "slow-valid":
                    # 分成 65 份，每秒一份，实际超过旧 60 秒总时限且保持高于 5 KB/s。
                    step = (len(content) + 64) // 65
                    for offset in range(0, len(content), step):
                        await response.write(content[offset : offset + step])
                        await asyncio.sleep(1)
                else:
                    await response.write(content)
                await response.write_eof()
            except ConnectionResetError, RuntimeError:
                pass
            return response
        return web.Response(body=content)

    async def test_http_and_package_errors_switch_once(self):
        """HTTP、摘要及 ZIP 错误都换源，最后有效源才能激活。"""
        self.modes.update(github="404", edgeone="bad-check", hongkong="html")
        await self.assets.retry()
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual(
            [source for source, name in self.requests if name.endswith("sha256")],
            ["github", "edgeone", "hongkong", "ghproxy"],
        )
        self.assertEqual(self.assets.download["source_index"], 4)
        self.assertEqual(self.assets.download["received_bytes"], (self.package / "webui.zip").stat().st_size)
        self.assertEqual(list(self.assets.cache.glob(".staging-*")), [])

    async def test_manual_failure_then_auto_and_cached_restart(self):
        """指定源失败不换源，下一次无参数重试回到自动；匹配缓存不读取网络。"""
        self.modes["hongkong"] = "403"
        await (await self.assets.request_retry("hongkong"))
        self.assertEqual(self.assets.state, "missing")
        self.assertEqual(self.requests, [("hongkong", "webui.zip.sha256")])
        self.assertFalse(self.assets.download["automatic"])
        await self.assets.retry()
        self.assertEqual(self.assets.state, "ready")
        restarted = FrontendAssets(self.root)
        with patch.object(restarted, "sources", side_effect=AssertionError("缓存不应读取源")):
            await restarted.retry()
        self.assertEqual(restarted.state, "ready")
        await restarted.close()

    async def test_no_length_and_truncated_response(self):
        """截断的已知长度响应换源，未知大小的完整响应仍能通过校验。"""
        self.modes.update(github="truncated", edgeone="unknown")
        await self.assets.retry()
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual(self.assets.download["source_name"], "EdgeOne 加速")
        self.assertIsNone(self.assets.download["total_bytes"])

    async def test_low_speed_and_stall_watchdog_switch(self):
        """持续滴流与完全无字节都由独立检测结束；测试缩短时间常量。"""
        self.modes.update(github="drip", edgeone="stall")
        with (
            patch("src.webui.frontend_assets.SAMPLE_SECONDS", 0.02),
            patch("src.webui.frontend_assets.LOW_SPEED_SECONDS", 0.12),
        ):
            async with asyncio.timeout(5):
                await self.assets.retry()
        self.release.set()
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual(self.assets.download["source_name"], "香港加速")
        self.assertFalse(
            any(task.get_name() in {"webui-archive-receive", "webui-archive-speed"} for task in asyncio.all_tasks())
        )

    async def test_close_stalled_receive_stops_watchdog_without_next_source(self):
        """关闭中断实际网络读取，并回收速度任务和暂存，不再换源。"""
        self.modes["github"] = "stall"
        self.assets.retry()
        await asyncio.wait_for(self.opened.wait(), 5)
        await self.assets.close()
        self.release.set()
        self.assertEqual({source for source, _ in self.requests}, {"github"})
        self.assertFalse(
            any(task.get_name() in {"webui-archive-receive", "webui-archive-speed"} for task in asyncio.all_tasks())
        )
        self.assertEqual(list(self.assets.cache.glob(".staging-*")), [])

    async def test_disk_error_does_not_switch(self):
        """成功下载后遇磁盘失败只结束本轮，不反复换站下载。"""
        with patch.object(self.assets, "_prepare_target", side_effect=PermissionError("synthetic")):
            await self.assets.retry()
        self.assertEqual(self.assets.state, "missing")
        self.assertEqual({source for source, _ in self.requests}, {"github"})
        self.assertIn("磁盘", self.assets.error)

    async def test_wrong_manifest_and_missing_entry_switch(self):
        """完整下载后的清单和入口校验失败也能换源。"""
        from src.webui.frontend_contract import extract_archive

        attempts = 0

        def reject_initial_packages(*args):
            """仅向前两次实际解包注入已知的清单及缺入口错误。"""
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ValueError("synthetic wrong build")
            if attempts == 2:
                raise FileNotFoundError("synthetic missing entry")
            return extract_archive(*args)

        with patch("src.webui.frontend_assets.extract_archive", reject_initial_packages):
            await self.assets.retry()
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual(attempts, 3)

    async def test_authenticated_selection_conflict_and_stale_source(self):
        """源列表只读且需认证；活动换源拒绝，失效 ID 不触发下载。"""
        app = FastAPI()

        async def auth(authorization: str | None = Header(None)):
            """复用接口认证依赖边界，真实 Cookie 在宿主集成验证。"""
            if authorization != "Bearer synthetic":
                raise HTTPException(401)

        register_frontend_routes(app, self.assets, auth)
        headers = {"Authorization": "Bearer synthetic"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            self.assertEqual((await client.get("/api/webui/frontend/sources")).status_code, 401)
            response = await client.get("/api/webui/frontend/sources", headers=headers)
            self.assertEqual(len(response.json()["sources"]), 4)
            self.assertNotIn("https://", response.text)
            self.assertFalse((self.root / "data").exists())
            self.assertEqual(self.requests, [])
            self.assertEqual(
                (
                    await client.post("/api/webui/frontend/retry", json={"source_id": "http://bad"}, headers=headers)
                ).status_code,
                400,
            )
            self.assertEqual(
                (
                    await client.post("/api/webui/frontend/retry", json={"url": "http://bad"}, headers=headers)
                ).status_code,
                422,
            )
            self.assertIsNone(self.assets._task)
            self.modes["github"] = "stall"
            self.assertEqual((await client.post("/api/webui/frontend/retry", headers=headers)).status_code, 202)
            await asyncio.wait_for(self.opened.wait(), 5)
            task = self.assets._task
            self.assertEqual((await client.post("/api/webui/frontend/retry", headers=headers)).status_code, 202)
            self.assertIs(self.assets._task, task)
            self.assertEqual(
                (
                    await client.post("/api/webui/frontend/retry", json={"source_id": "edgeone"}, headers=headers)
                ).status_code,
                409,
            )
            await self.assets.close()
            self.release.set()

        other = FrontendAssets(self.root)
        self.addAsyncCleanup(other.close)
        config = self.root / "data/webui.json"
        config.write_text(
            json.dumps(
                {"git_mirrors": [{"clone_prefix": "https://custom.example/https://github.com", "enabled": True}]}
            ),
            encoding="utf-8",
        )
        original = await other.sources()
        custom = next(source for source in original if source.id.startswith("configured-"))
        self.assertEqual([source.id for source in await other.sources()], [source.id for source in original])
        config.write_text('{"git_mirrors": []}', encoding="utf-8")
        with self.assertRaises(ValueError):
            await other.request_retry(custom.id)
        self.assertIsNone(other._task)

    async def test_real_download_over_sixty_seconds_keeps_progress(self):
        """实际传输超过旧时限，每秒有进度且高于低速阈值，最终正常激活。"""
        (self.package / "dist/assets/payload.bin").write_bytes(random.Random(42).randbytes(700_000))
        package_directory(self.root, self.package / "dist", self.package, "a" * 40)
        self.modes["github"] = "slow-valid"
        started = asyncio.get_running_loop().time()
        task = self.assets.retry()
        await asyncio.wait_for(self.opened.wait(), 5)
        async with asyncio.timeout(90):
            while not task.done():
                await asyncio.sleep(1)
                if asyncio.get_running_loop().time() - started > 60:
                    self.assertGreater(self.assets.download["received_bytes"], 300_000)
                    self.assertGreater(self.assets.download["bytes_per_second"], 5000)
            await task
        self.assertGreater(asyncio.get_running_loop().time() - started, 60)
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual({source for source, _ in self.requests}, {"github"})
