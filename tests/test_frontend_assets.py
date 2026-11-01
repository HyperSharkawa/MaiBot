"""使用临时源码和本地 HTTP 验证资源状态机、真实解包与恢复 API。"""

import asyncio
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
import httpx
from aiohttp import web
from fastapi import FastAPI, Header, HTTPException

from src.webui.frontend_assets import FrontendAssets
from src.webui.frontend_contract import MANIFEST
from src.webui.frontend_contract import extract_archive
from src.webui.frontend_routes import register_frontend_routes
from test_frontend_contract import small_package, source_copy


class AssetsTests(unittest.IsolatedAsyncioTestCase):
    """每次测试只操作自己的目录和进程内 HTTP 服务。"""

    async def asyncSetUp(self):
        """准备旧页面与有效新包，下载默认由本例替身提供。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.root = source_copy(self.work / "source")
        self.package = self.work / "package"
        self.identity = small_package(self.root, self.package)
        self.assets = FrontendAssets(self.root)
        self.addAsyncCleanup(self.assets.close)
        self.downloads = 0

    async def deliver(self, destination):
        """模拟网络交付真实包；计数用于证明没有隐式重试。"""
        self.downloads += 1
        shutil.copyfile(self.package / "webui.zip", destination)

    def legacy(self):
        """创建没有新清单的旧版页面作为回退候选。"""
        target = self.root / "webui/dist"
        shutil.copytree(self.package / "dist", target)
        (target / MANIFEST).unlink()
        return target

    async def test_install_reload_offline_and_disabled(self):
        """构造时无任务；成功安装后重建管理器，不联网即可启用匹配缓存。"""
        self.assertIsNone(self.assets._task)
        self.assertFalse(self.assets.cache.exists())
        with patch.object(self.assets, "_download", self.deliver):
            await self.assets.retry()
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.assets.current_id, self.identity)
        second = FrontendAssets(self.root)
        with patch.object(second, "_download", side_effect=AssertionError("不应下载")):
            await second.retry()
        self.assertEqual(second.state, "ready")
        await second.close()
        self.assertEqual(list(self.assets.cache.glob(".staging-*")), [])

    async def test_legacy_failure_retry_and_single_flight(self):
        """失败保留旧页面；并发重试只产生一个任务；成功无需重启。"""
        legacy = self.legacy()
        with patch.object(self.assets, "_download", side_effect=TimeoutError):
            await self.assets.retry()
        self.assertEqual(self.assets.state, "stale")
        self.assertEqual(self.assets.active, legacy)
        self.assertTrue(self.assets.status()["using_old_page"])
        with patch.object(self.assets, "_download", self.deliver):
            task = self.assets.retry()
            self.assertIs(task, self.assets.retry())
            await task
        self.assertEqual(self.downloads, 1)
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual(self.assets.previous, legacy)

    async def test_corrupt_package_exhausts_sources_without_another_round(self):
        """各源损坏 ZIP 只尝试一次，不留下任务异常或自动开始第二轮。"""

        async def corrupt(destination):
            """提供损坏的下载内容。"""
            self.downloads += 1
            destination.write_bytes(b"not a zip")

        with patch.object(self.assets, "_download", corrupt):
            await self.assets.retry()
            await asyncio.sleep(0)
            self.assertEqual(self.assets.status()["state"], "missing")
        self.assertEqual(self.downloads, 4)
        self.assertFalse((self.assets.cache / "current.json").exists())

    async def test_non_object_cache_pointer_and_manifest_are_recoverable(self):
        """合法 JSON 但结构损坏的缓存也必须结束检查，并允许重新下载。"""
        legacy = self.legacy()
        self.assets.cache.mkdir(parents=True)
        (self.assets.cache / "current.json").write_text("[]", encoding="utf-8")
        (legacy / MANIFEST).write_text("[]", encoding="utf-8")
        with patch.object(self.assets, "_download", self.deliver):
            await self.assets.retry()
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual(self.downloads, 1)

    async def test_cancel_download_retains_legacy_and_cleans(self):
        """关闭只取消自己的下载并清理暂存，旧资源继续留在磁盘。"""
        legacy = self.legacy()
        entered = asyncio.Event()

        async def blocked(destination):
            """用显式事件模拟尚未下载完的连接。"""
            destination.write_bytes(b"partial")
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.assets, "_download", blocked):
            self.assets.retry()
            await entered.wait()
            await self.assets.close()
        self.assertTrue((legacy / "index.html").exists())
        self.assertEqual(list(self.assets.cache.glob(".staging-*")), [])

    async def test_pointer_failure_preserves_active_page(self):
        """指针写入失败时不切换页面、不破坏旧资源。"""
        legacy = self.legacy()
        original = Path.replace

        def reject_pointer(path, target):
            """只注入当前指针的原子替换失败。"""
            if path.name == "current.json.tmp":
                raise OSError("disk full")
            return original(path, target)

        with patch.object(self.assets, "_download", self.deliver), patch.object(Path, "replace", reject_pointer):
            await self.assets.retry()
        self.assertEqual(self.assets.active, legacy)
        self.assertEqual(self.assets.state, "stale")
        self.assertFalse((self.assets.cache / "current.json.tmp").exists())

    async def test_close_during_extraction_waits_without_activation(self):
        """线程解包未完成时关闭必须等待，不与清理竞态，也不能在关闭后切换资源。"""
        entered = threading.Event()
        release = threading.Event()

        def delayed_extract(*args):
            """用事件暂停解包，模拟取消时线程仍在写磁盘。"""
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release missing")
            return extract_archive(*args)

        with (
            patch.object(self.assets, "_download", self.deliver),
            patch("src.webui.frontend_assets.extract_archive", delayed_extract),
        ):
            self.assets.retry()
            await asyncio.to_thread(entered.wait, 5)
            closing = asyncio.create_task(self.assets.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            release.set()
            await closing
        self.assertFalse((self.assets.cache / "current.json").exists())
        self.assertEqual(list(self.assets.cache.glob(".staging-*")), [])

    async def test_keep_previous_managed_version(self):
        """切换后保留上一版，重启后仍可取旧资源，其他文件不被清理。"""
        with patch.object(self.assets, "_download", self.deliver):
            await self.assets.retry()
        previous = self.assets.active
        (self.root / "webui/src/example.ts").write_text("new", encoding="utf-8")
        new_package = self.work / "new-package"
        new_id = small_package(self.root, new_package)
        self.package = new_package
        next_assets = FrontendAssets(self.root)
        self.addAsyncCleanup(next_assets.close)
        unrelated = next_assets.cache / "releases/notes"
        unrelated.mkdir()
        with patch.object(next_assets, "_download", self.deliver):
            await next_assets.retry()
        self.assertEqual(next_assets.current_id, new_id)
        self.assertEqual(next_assets.previous, previous)
        self.assertTrue(unrelated.exists())
        restarted = FrontendAssets(self.root)
        await restarted.retry()
        self.assertEqual(restarted.previous, previous)
        await restarted.close()

    async def test_concurrent_close_and_cancelled_waiter_do_not_interrupt_file_work(self):
        """并发关闭和调用方取消只通知下载退出一次，文件线程完成前不能清理暂存。"""
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def delayed_extract(*args):
            """保持解包线程存活，检查多个关闭入口的真实等待关系。"""
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release missing")
            try:
                return extract_archive(*args)
            finally:
                finished.set()

        with (
            patch.object(self.assets, "_download", self.deliver),
            patch("src.webui.frontend_assets.extract_archive", delayed_extract),
        ):
            task = self.assets.retry()
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            first = asyncio.create_task(self.assets.close())
            second = asyncio.create_task(self.assets.close())
            try:
                await asyncio.sleep(0)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertFalse(second.done())
                self.assertFalse(finished.is_set())
                self.assertEqual(task.cancelling(), 1)
                self.assertEqual(len(list(self.assets.cache.glob(".staging-*"))), 1)
            finally:
                release.set()
                await second
        self.assertTrue(finished.is_set())
        self.assertIsNone(self.assets.active)
        self.assertFalse((self.assets.cache / "current.json").exists())
        self.assertEqual(list(self.assets.cache.glob(".staging-*")), [])

    async def test_matching_bundled_resources_and_unactivated_staging(self):
        """匹配自带资源可离线使用，尚未激活的暂存目录不能冒充可用页面。"""
        staging = self.assets.cache / ".staging-interrupted"
        staging.mkdir(parents=True)
        shutil.copytree(self.package / "dist", staging / "contents")
        with patch.object(self.assets, "_download", side_effect=TimeoutError):
            await self.assets.retry()
        self.assertEqual(self.assets.state, "missing")
        shutil.copytree(self.package / "dist", self.root / "webui/dist")
        with patch.object(self.assets, "_download", side_effect=AssertionError("自带匹配资源不应下载")):
            await self.assets.retry()
        self.assertEqual(self.assets.state, "ready")
        self.assertEqual(self.assets.active, self.root / "webui/dist")

    async def test_served_page_carries_its_actual_version(self):
        """旧 HTML 与首次状态查询之间发生更新时，版本标记仍指向发送过的旧页面。"""
        app = FastAPI()

        async def auth():
            """本例只读取页面，不调用受保护的写入口。"""
            raise AssertionError("不应执行认证写入口")

        register_frontend_routes(app, self.assets, auth)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            missing = await client.get("/")
            self.assertIn('data-page-version="missing"', missing.text)
            self.assets.active = self.legacy()
            stale = await client.get("/config/bot")
            self.assertIn('data-page-version="legacy"', stale.text)
            with patch.object(self.assets, "_download", self.deliver):
                await self.assets.retry()
            ready = await client.get("/config/bot")
            self.assertIn(f'data-page-version="{self.identity}"', ready.text)
            self.assertIn('data-page-version="legacy"', stale.text)
            self.assertEqual((await client.get("/api/webui/frontend/status")).json()["current_id"], self.identity)

    async def test_real_http_checksum_404_and_limit(self):
        """保留真实 aiohttp 客户端，只把固定下载地址映射到本地 HTTP。"""
        mode = "valid"

        async def serve(request):
            """提供正常、404、错误校验值三类网络返回。"""
            if mode == "missing":
                raise web.HTTPNotFound()
            name = request.match_info["name"]
            if mode == "checksum" and name.endswith("sha256"):
                return web.Response(text="0" * 64 + "  webui.zip\n")
            return web.FileResponse(self.package / name)

        app = web.Application()
        app.router.add_get("/{name}", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        original = aiohttp.ClientSession

        def local_session(**kwargs):
            """仅测试期间改写本例客户端 URL，仍执行真实流式下载。"""
            session = original(**kwargs)
            get = session.get
            session.get = lambda url: get(f"http://127.0.0.1:{port}/{url.rsplit('/', 1)[1]}")
            return session

        self.assets.expected_id = self.identity
        with patch("src.webui.frontend_assets.aiohttp.ClientSession", local_session):
            await self.assets._download(self.work / "download.zip")
            mode = "checksum"
            with self.assertRaises(ValueError):
                await self.assets._download(self.work / "bad-check.zip")
            mode = "missing"
            await self.assets.retry()
            self.assertEqual(self.assets.state, "missing")
            self.assertIn("尚未发布", self.assets.error)
            mode = "valid"
            with patch("src.webui.frontend_assets.MAX_ARCHIVE_BYTES", 1), self.assertRaises(ValueError):
                await self.assets._download(self.work / "oversized.zip")

    async def test_routes_auth_missing_asset_and_service_continuity(self):
        """恢复接口无须 React；重试需认证，其他 API 与静态 404 保持可用。"""
        app = FastAPI()

        async def auth(authorization: str | None = Header(None)):
            """接口单元测试的认证依赖；真实 Cookie/Bearer 集成另在隔离宿主验证。"""
            if authorization != "Bearer synthetic":
                raise HTTPException(401)

        @app.get("/api/health")
        async def health():
            """确认下载未影响相邻 API。"""
            return {"ok": True}

        register_frontend_routes(app, self.assets, auth)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            self.assertIn("管理页面尚未准备好", (await client.get("/")).text)
            self.assertEqual((await client.get("/assets/missing.js")).status_code, 404)
            self.assertEqual((await client.get("/api/not-existing")).status_code, 404)
            self.assertEqual((await client.post("/api/webui/frontend/retry")).status_code, 401)
            self.assertIsNone(self.assets._task)
            with patch.object(self.assets, "_download", self.deliver):
                response = await client.post("/api/webui/frontend/retry", headers={"Authorization": "Bearer synthetic"})
                self.assertEqual(response.status_code, 202)
                self.assertTrue((await client.get("/api/health")).json()["ok"])
                await self.assets._task
            self.assertEqual((await client.get("/api/webui/frontend/status")).json()["state"], "ready")
            page = await client.get("/config/bot")
            self.assertIn("/api/webui/frontend/recovery.js", page.text)
            self.assertEqual(page.headers["cache-control"], "no-store")
            self.assertEqual((await client.get("/assets/main.js")).status_code, 200)
            self.assertEqual((await client.get("/%2e%2e/LICENSE")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
