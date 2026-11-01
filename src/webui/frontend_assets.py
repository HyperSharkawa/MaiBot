"""管理前端资源的一次性下载、校验和原子切换；不依赖业务配置。"""

import asyncio
import hashlib
import json
import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import aiohttp

from .frontend_contract import (
    BUILD_ID_PATTERN,
    MANIFEST,
    MAX_ARCHIVE_BYTES,
    REPOSITORY,
    build_identity,
    extract_archive,
    json_bytes,
    validate_directory,
    validate_entry,
)


class FrontendAssets:
    """拥有本次 WebUI 生命周期的下载任务；实例化不联网、不写磁盘。"""

    def __init__(self, root: Path, logger=None):
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

    def status(self) -> dict:
        """返回不包含路径、Token 或网络异常原文的页面状态。"""
        return {
            "state": self.state,
            "expected_id": self.expected_id,
            "current_id": self.current_id,
            "using_old_page": self.active is not None and self.current_id != self.expected_id,
            "error": self.error,
        }

    def retry(self) -> asyncio.Task | None:
        """创建或复用唯一下载任务；就绪或关闭后不发起请求。"""
        if self._closed or self.state == "ready":
            return self._task
        if self._task is None or self._task.done():
            self.state = "checking"
            self.error = None
            self._task = asyncio.create_task(self._attempt(), name="webui-frontend-download")
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
        """从固定仓库下载指定构建和校验文件；使用系统代理，合计限制 60 秒。"""
        base = f"https://github.com/{REPOSITORY}/releases/download/webui-{self.expected_id}"
        timeout = aiohttp.ClientTimeout(total=60, connect=10)
        async with asyncio.timeout(60), aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(f"{base}/webui.zip.sha256") as response:
                response.raise_for_status()
                check = bytearray()
                async for chunk in response.content.iter_chunked(1024):
                    check.extend(chunk)
                    if len(check) > 1024:
                        raise ValueError("前端校验文件格式错误")
                parts = check.decode("ascii").split()
                if len(parts) != 2 or not BUILD_ID_PATTERN.fullmatch(parts[0]) or parts[1] != "webui.zip":
                    raise ValueError("前端校验文件格式错误")
            async with session.get(f"{base}/webui.zip") as response:
                response.raise_for_status()
                if response.content_length is not None and response.content_length > MAX_ARCHIVE_BYTES:
                    raise ValueError("前端下载包超过大小限制")
                size = 0
                with destination.open("xb") as output:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise ValueError("前端下载包超过大小限制")
                        output.write(chunk)
            if await self._file_work(self._archive_digest, destination) != parts[0]:
                raise ValueError("前端下载包校验失败")

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

    async def _attempt(self) -> None:
        """执行一次尝试；下载失败保留完整旧页面，不安排自动重试。"""
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
            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=self.cache))
            archive = staging / "webui.zip"
            self.state = "downloading"
            await self._download(archive)
            self.state = "installing"
            extracted = staging / "contents"
            extracted.mkdir()
            await self._file_work(extract_archive, archive, extracted, self.expected_id)
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
        ) as error:
            if isinstance(error, aiohttp.ClientResponseError) and error.status == 404:
                self.error = "当前版本的页面尚未发布，请稍后点击重试下载。"
            elif isinstance(error, (aiohttp.ClientError, TimeoutError)):
                self.error = "页面下载失败，请检查网络后重试。"
            elif isinstance(error, OSError):
                self.error = "页面文件无法读取或保存，请检查磁盘空间及目录权限。"
            else:
                self.error = "页面资源校验失败，已保留原有页面，请重试下载。"
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
