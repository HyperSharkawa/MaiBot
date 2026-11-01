"""管理前端资源的一次性下载、校验和原子切换；不依赖业务配置。"""

import asyncio
import hashlib
import json
import logging
import shutil
import tempfile
import zipfile
import zlib
from dataclasses import replace
from pathlib import Path

import aiohttp

from .frontend_contract import (
    BUILD_ID_PATTERN,
    MANIFEST,
    MAX_ARCHIVE_BYTES,
    build_identity,
    extract_archive,
    json_bytes,
    validate_directory,
    validate_entry,
)
from .frontend_sources import GITHUB, DownloadSource, read_sources
from .frontend_transfer import LOW_SPEED_SECONDS, SAMPLE_SECONDS, LowSpeedError, TransferMeter


class DownloadBusyError(ValueError):
    """已有下载正在执行，不能中途切换另一个源。"""


class DownloadSourcesError(ValueError):
    """下载源配置或本轮候选均无法提供可用资源。"""


class FrontendAssets:
    """拥有本次 WebUI 生命周期的下载任务；实例化不联网、不写磁盘。"""

    def __init__(self, root: Path, logger=None):
        """建立当前服务器的资源状态，不联网或写配置。"""
        self.root = root.resolve()
        self.cache = self.root / "data/webui"
        self.logger = logger or logging.getLogger(__name__)
        self.expected_id: str | None = None
        self.current_id: str | None = None
        self.active: Path | None = None
        self.previous: Path | None = None
        self.state = "checking"
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._closed = False
        self.download: dict | None = None
        self._selection = "auto"
        self._sources: list[DownloadSource] = []
        self._source = DownloadSource("github", "GitHub", GITHUB)

    def status(self) -> dict:
        """返回不包含路径、Token 或网络异常原文的页面状态。"""
        return {
            "state": self.state,
            "expected_id": self.expected_id,
            "current_id": self.current_id,
            "using_old_page": self.active is not None and self.current_id != self.expected_id,
            "error": self.error,
            "download": dict(self.download) if self.download else None,
        }

    async def sources(self) -> list[DownloadSource]:
        """重新只读获取下载源；相同前缀复用实例内 ID，删除或换址后旧 ID 失效。"""
        candidates = await self._file_work(read_sources, self.root, self.logger)
        previous = {source.prefix: source.id for source in self._sources}
        self._sources = [replace(source, id=previous.get(source.prefix, source.id)) for source in candidates]
        return list(self._sources)

    async def request_retry(self, source_id: str = "auto") -> asyncio.Task | None:
        """供认证接口使用；先验证当前源列表，再创建一次下载，失效选择不启动任务。"""
        if self._closed or self.state == "ready" or (self._task and not self._task.done()):
            return self.retry(source_id)
        sources = await self.sources()
        if source_id != "auto" and not any(source.id == source_id for source in sources):
            raise ValueError("下载源已失效或被禁用，请重新选择。")
        return self.retry(source_id, sources)

    def retry(self, source_id: str = "auto", sources: list[DownloadSource] | None = None) -> asyncio.Task | None:
        """创建或复用唯一下载任务；就绪或关闭后不发起请求。"""
        if self._closed or self.state == "ready":
            return self._task
        if self._task and not self._task.done() and source_id != self._selection:
            raise DownloadBusyError("本次下载仍在进行，请完成后再选择其他源。")
        if self._task is None or self._task.done():
            self.state = "checking"
            self.error = None
            self.download = None
            self._selection = source_id
            self._task = asyncio.create_task(self._attempt(sources), name="webui-frontend-download")
        return self._task

    async def _file_work(self, function, *args):
        """在线程中检查或解包；取消时等待磁盘操作结束，避免删除仍在写入的目录。"""
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _local_candidates(self) -> tuple[str, list[tuple[Path, str | None]]]:
        """按匹配缓存、匹配自带资源、旧缓存、旧自带资源的顺序返回完整候选。"""
        expected, _, _ = build_identity(self.root)
        paths = [self.cache / "releases" / expected]
        pointer = self.cache / "current.json"
        try:
            saved = json.loads(pointer.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise ValueError("前端缓存指针必须为对象")
            for key in ("current", "previous"):
                identity = saved.get(key)
                if isinstance(identity, str) and BUILD_ID_PATTERN.fullmatch(identity):
                    paths.append(self.cache / "releases" / identity)
        except OSError, ValueError, TypeError:
            pass
        bundled = self.root / "webui/dist"
        paths.append(bundled)
        valid = []
        for path in dict.fromkeys(paths):
            try:
                if path.is_symlink():
                    continue
                if (path / MANIFEST).exists():
                    metadata = validate_directory(path)
                    valid.append((path, metadata["build_id"]))
                elif path == bundled:
                    validate_entry(path)
                    valid.append((path, None))
            except OSError, ValueError, KeyError, TypeError:
                continue
        valid.sort(key=lambda item: item[1] != expected)
        return expected, valid

    async def _download(self, destination: Path) -> None:
        """下载当前源的校验文件及 ZIP；整包不限时，持续低速或断流才结束。"""
        base = self._source.base_url(self.expected_id)
        if self.download is None:
            self._set_progress(1, 1)
        timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)
        async with aiohttp.ClientSession(
            timeout=timeout, trust_env=True, headers={"Accept-Encoding": "identity"}
        ) as session:
            self.download["phase"] = "checksum"
            async with asyncio.timeout(60), session.get(f"{base}/webui.zip.sha256") as response:
                response.raise_for_status()
                check = bytearray()
                async for chunk in response.content.iter_chunked(1024):
                    check.extend(chunk)
                    if len(check) > 1024:
                        raise ValueError("前端校验文件格式错误")
                parts = check.decode("ascii").split()
                if len(parts) != 2 or not BUILD_ID_PATTERN.fullmatch(parts[0]) or parts[1] != "webui.zip":
                    raise ValueError("前端校验文件格式错误")
            self.download["phase"] = "connecting"
            async with session.get(f"{base}/webui.zip") as response:
                response.raise_for_status()
                if response.content_length is not None and response.content_length > MAX_ARCHIVE_BYTES:
                    raise ValueError("前端下载包超过大小限制")
                self.download["phase"] = "archive"
                if response.headers.get("Content-Encoding", "identity").lower() == "identity":
                    self.download["total_bytes"] = response.content_length
                await self._receive_archive(response, destination)
            self.download["phase"] = "validating"
            if await self._file_work(self._archive_digest, destination) != parts[0]:
                raise ValueError("前端下载包校验失败")

    def _set_progress(self, index: int, count: int) -> None:
        """开始一个源时重置计数；只保留当前尝试的短状态。"""
        self.download = {
            "phase": "connecting",
            "source_name": self._source.name,
            "source_index": index,
            "source_count": count,
            "automatic": self._selection == "auto",
            "received_bytes": 0,
            "total_bytes": None,
            "bytes_per_second": 0,
            "low_speed_seconds": 0,
        }

    async def _receive_archive(self, response: aiohttp.ClientResponse, destination: Path) -> None:
        """同时接收 ZIP 和检查低速；退出时等待两个子任务，避免遗留检测任务。"""
        meter = TransferMeter(asyncio.get_running_loop().time())

        async def receive():
            """流式写入当前尝试的临时 ZIP，不保留已处理的数据块。"""
            with destination.open("xb") as output:
                async for chunk in response.content.iter_chunked(64 * 1024):
                    self.download["received_bytes"] += len(chunk)
                    if self.download["received_bytes"] > MAX_ARCHIVE_BYTES:
                        raise ValueError("前端下载包超过大小限制")
                    output.write(chunk)

        async def watch():
            """没有新数据时也持续采样，低速时间达到上限即结束当前源。"""
            while True:
                await asyncio.sleep(SAMPLE_SECONDS)
                speed, low_seconds = meter.sample(asyncio.get_running_loop().time(), self.download["received_bytes"])
                self.download.update(bytes_per_second=round(speed, 1), low_speed_seconds=round(low_seconds, 1))
                if low_seconds >= LOW_SPEED_SECONDS:
                    raise LowSpeedError("持续 60 秒低于 5 KB/s")

        receiver = asyncio.create_task(receive(), name="webui-archive-receive")
        watcher = asyncio.create_task(watch(), name="webui-archive-speed")
        try:
            done, _ = await asyncio.wait((receiver, watcher), return_when=asyncio.FIRST_COMPLETED)
            # 完整响应已读完时立即交给校验，最后一秒的低速不应否定完成的下载。
            await (receiver if receiver in done else watcher)
        finally:
            for task in (receiver, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(receiver, watcher, return_exceptions=True)

    @staticmethod
    def _archive_digest(path: Path) -> str:
        """在文件工作线程中流式计算摘要，避免阻塞事件循环或整包读入内存。"""
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def _prepare_target(self) -> None:
        """清理此前检查已排除的同版本损坏缓存，大目录删除放在文件工作线程。"""
        releases = self.cache / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        target = releases / self.expected_id
        if target.exists():
            shutil.rmtree(target)

    def _activate(self, extracted: Path) -> None:
        """先放置完整目录，再原子替换指针；失败时继续使用原页面。"""
        releases = self.cache / "releases"
        target = releases / self.expected_id
        extracted.replace(target)
        previous_id = self.current_id if self.active and self.active.parent == releases else None
        pointer_temp = self.cache / "current.json.tmp"
        try:
            pointer_temp.write_bytes(json_bytes({"current": self.expected_id, "previous": previous_id}))
            pointer_temp.replace(self.cache / "current.json")
        finally:
            pointer_temp.unlink(missing_ok=True)
        self.previous = self.active
        self.active = target
        self.current_id = self.expected_id

    def _prune(self) -> None:
        """只清理托管版本目录，保留当前和上一版供已打开的页面继续取资源。"""
        releases = self.cache / "releases"
        keep = {self.active, self.previous}
        for path in releases.iterdir():
            if path not in keep and not path.is_symlink() and path.is_dir() and BUILD_ID_PATTERN.fullmatch(path.name):
                shutil.rmtree(path)

    async def _attempt(self, sources: list[DownloadSource] | None = None) -> None:
        """执行一轮有限换源；完整校验后才激活，不安排下一轮自动重试。"""
        staging = None
        try:
            self.expected_id, candidates = await self._file_work(self._local_candidates)
            if candidates:
                self.active, self.current_id = candidates[0]
                self.previous = candidates[1][0] if len(candidates) > 1 else None
                if self.current_id == self.expected_id:
                    self.state = "ready"
                    return
            self.cache.mkdir(parents=True, exist_ok=True)
            sources = sources if sources is not None else await self.sources()
            if self._selection != "auto":
                sources = [source for source in sources if source.id == self._selection]
            if not sources:
                raise DownloadSourcesError("没有已启用的 WebUI 下载源，请检查镜像配置。")
            last_error = None
            all_missing = True
            for index, source in enumerate(sources, 1):
                if self._closed:
                    return
                self._source = source
                self._set_progress(index, len(sources))
                self.state = "downloading"
                staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.cache))
                archive = staging / "webui.zip"
                extracted = staging / "contents"
                self.logger.info(f"WebUI 正在尝试下载源 {source.name}（{index}/{len(sources)}）")
                try:
                    await self._download(archive)
                    self.state = "installing"
                    self.download["phase"] = "validating"
                    extracted.mkdir()
                    try:
                        await self._file_work(extract_archive, archive, extracted, self.expected_id)
                    except FileNotFoundError as error:
                        # 校验引用的文件缺失属于包错误；写盘失败等其他 OSError 必须停止整轮。
                        raise ValueError("前端包入口资源缺失") from error
                    break
                except (
                    aiohttp.ClientError,
                    TimeoutError,
                    ValueError,
                    KeyError,
                    TypeError,
                    zipfile.BadZipFile,
                    zlib.error,
                ) as error:
                    last_error = error
                    all_missing &= isinstance(error, aiohttp.ClientResponseError) and error.status == 404
                    reason = "持续低速" if isinstance(error, LowSpeedError) else type(error).__name__
                    self.logger.warning(f"WebUI 下载源 {source.name} 失败（{reason}）")
                    await self._file_work(shutil.rmtree, staging)
                    staging = None
            else:
                if not all_missing and isinstance(last_error, aiohttp.ClientResponseError) and last_error.status == 404:
                    raise DownloadSourcesError("所有下载源均未能提供可用页面，请检查网络或选择其他源重试。")
                raise last_error
            await self._file_work(self._prepare_target)
            if not self._closed:
                self._activate(extracted)
                self.state = "ready"
                self.logger.info(f"WebUI 前端资源已就绪：{self.expected_id}")
                try:
                    await self._file_work(self._prune)
                except OSError:
                    self.logger.warning("WebUI 已更新，旧缓存清理失败，将保留旧目录")
        except asyncio.CancelledError:
            raise
        except (
            aiohttp.ClientError,
            TimeoutError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            zipfile.BadZipFile,
            zlib.error,
        ) as error:
            if isinstance(error, DownloadSourcesError):
                self.error = str(error)
            elif isinstance(error, LowSpeedError):
                self.error = "下载持续 60 秒低于 5 KB/s，请选择其他下载源重试。"
            elif isinstance(error, aiohttp.ClientResponseError) and error.status == 404:
                self.error = "当前版本的页面尚未发布，请稍后点击重试下载。"
            elif isinstance(error, (aiohttp.ClientError, TimeoutError)):
                self.error = "页面下载失败，请检查网络后重试。"
            elif isinstance(error, OSError):
                self.error = "页面文件无法读取或保存，请检查磁盘空间及目录权限。"
            else:
                self.error = "页面资源校验失败，请选择其他下载源重试。"
            self.state = "stale" if self.active else "missing"
            self.logger.warning(f"WebUI 资源未更新（{type(error).__name__}）：{self.error}")
        finally:
            if staging is not None:
                try:
                    await self._file_work(shutil.rmtree, staging)
                except OSError:
                    self.logger.warning("WebUI 临时资源目录清理失败")

    async def close(self) -> None:
        """所有关闭入口共用清理任务；调用方取消不能再次中断正在退出的解包。"""
        self._closed = True
        if self._task is None:
            return
        if self._close_task is None:
            # 下载也可能已被宿主取消，重复取消会打断它等待文件线程的清理过程。
            if not self._task.done() and not self._task.cancelling():
                self._task.cancel()
            self._close_task = asyncio.create_task(self._finish_close(), name="webui-frontend-close")
        await asyncio.shield(self._close_task)

    async def _finish_close(self) -> None:
        """只等待本实例的下载任务完成一次清理，保留非取消类错误。"""
        try:
            await self._task
        except asyncio.CancelledError:
            pass
