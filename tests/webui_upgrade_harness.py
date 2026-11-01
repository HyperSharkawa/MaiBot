"""在临时 Git 仓库验证取消产物跟踪后的真实更新；不修改被测仓库历史。"""

import argparse
import asyncio
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PREFIXES = ("src/", "tests/", "template/", "webui/", ".github/workflows/")
SOURCE_FILES = {
    ".gitignore",
    ".gitattributes",
    ".dockerignore",
    "Dockerfile",
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    "requirements.txt",
    "bot.py",
    "scripts/build_webui.py",
    "scripts/publish_webui.py",
}


def git(directory: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """在指定的临时仓库运行真实 Git，测试提交只使用虚构作者。"""
    return subprocess.run(
        ["git", "-c", "user.name=WebUI tests", "-c", "user.email=webui-tests@example.invalid", *args],
        cwd=directory,
        capture_output=True,
        check=check,
    )


def source_names() -> list[str]:
    """仅选择运行源码和测试所需文件，排除旧产物、配置、数据与私人记录。"""
    names = git(ROOT, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.decode().split("\0")
    return sorted(
        {
            name
            for name in names
            if name
            and not name.startswith("webui/dist/")
            and (name.startswith(SOURCE_PREFIXES) or name in SOURCE_FILES)
            and (ROOT / name).is_file()
        }
    )


def seed_personal_files(root: Path) -> dict[str, bytes]:
    """创建虚构配置、登录信息和真实 SQLite 文件，验证 Git 更新不改动它们。"""
    (root / "config").mkdir()
    (root / "data").mkdir()
    (root / "config/local.toml").write_text("enabled = true\n", encoding="utf-8")
    (root / "data/webui.json").write_text('{"access_token":"synthetic-upgrade-token"}', encoding="utf-8")
    with closing(sqlite3.connect(root / "data/test.db")) as database, database:
        database.execute("CREATE TABLE sample (value TEXT)")
        database.execute("INSERT INTO sample VALUES (?)", ("synthetic",))
    return {name: (root / name).read_bytes() for name in ("config/local.toml", "data/webui.json", "data/test.db")}


async def check_runtime(package: Path, recover: bool) -> None:
    """在更新后的真实源码中运行加载器，仅替换下载边界；禁用 Node/Bun 和外网。"""
    sys.path.insert(0, str(ROOT))
    from src.webui.frontend_assets import FrontendAssets
    from src.webui.frontend_contract import MANIFEST, build_identity

    assert shutil.which("node") is None and shutil.which("bun") is None
    assert not (ROOT / "webui/dist").exists()
    expected = build_identity(ROOT)[0]
    assets = FrontendAssets(ROOT)
    attempts = 0

    async def deliver(destination):
        """使用此前公开下载并校验的包，首次失败场景只在用户重试后交付。"""
        nonlocal attempts
        attempts += 1
        if recover and attempts == 1:
            raise TimeoutError("synthetic unavailable release")
        shutil.copyfile(package, destination)

    assets._download = deliver
    try:
        await assets.retry()
        if recover:
            assert assets.state == "missing"
            await assets.retry()
        assert assets.state == "ready" and assets.current_id == expected
        manifest = json.loads((assets.active / MANIFEST).read_text(encoding="utf-8"))
    finally:
        await assets.close()
    restarted = FrontendAssets(ROOT)

    async def forbidden(_destination):
        """匹配缓存的下一次启动不得联网。"""
        raise AssertionError("unexpected network")

    restarted._download = forbidden
    try:
        await restarted.retry()
        assert restarted.state == "ready"
    finally:
        await restarted.close()
    print(
        json.dumps(
            {
                "build_id": expected,
                "source_commit": manifest["source_commit"],
                "ready": True,
                "offline_restart": True,
                "attempts": attempts,
                "git_present": (ROOT / ".git").exists(),
            }
        )
    )


def runtime_child(root: Path, package: Path, recover: bool = False) -> dict:
    """启动候选自己的测试入口，移除 Node/Bun 命令搜索路径，不借用原工作区模块。"""
    environment = os.environ.copy()
    environment["PATH"] = ""
    command = [
        sys.executable,
        str(root / "tests/webui_upgrade_harness.py"),
        "--check-runtime",
        "--package",
        str(package),
    ]
    if recover:
        command.append("--recover")
    result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True, check=True, timeout=90)
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> None:
    """从含产物的历史提交构造临时远端和用户副本，验证真实快进更新及冲突保留。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before-ref")
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--check-runtime", action="store_true")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    package = args.package.resolve()
    if args.check_runtime:
        asyncio.run(check_runtime(package, args.recover))
        return
    if not args.before_ref:
        parser.error("需要 --before-ref 指定仍包含受跟踪 dist 的版本")
    with tempfile.TemporaryDirectory(prefix="webui-upgrade-") as temporary:
        workspace = Path(temporary)
        publisher = workspace / "publisher"
        publisher.mkdir()
        archive = git(
            ROOT,
            "archive",
            "--format=tar",
            args.before_ref,
            "--",
            "src",
            "tests",
            "template",
            "webui",
            ".github/workflows",
            *sorted(SOURCE_FILES),
        ).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(publisher, filter="data")
        git(publisher, "init", "--initial-branch=main")
        # 只对导出的历史快照建立初始索引，保留真实历史中已跟踪的锁文件和产物。
        git(publisher, "add", "--force", "--all")
        git(publisher, "commit", "-m", "test: before resource migration")
        old_files = git(publisher, "ls-files", "webui/dist").stdout.decode().splitlines()
        assert old_files
        remote = workspace / "remote.git"
        git(workspace, "clone", "--bare", str(publisher), str(remote))
        user = workspace / "user"
        dirty = workspace / "dirty"
        git(workspace, "clone", str(remote), str(user))
        git(workspace, "clone", str(remote), str(dirty))
        personal = seed_personal_files(user)
        modified = dirty / "webui/dist/index.html"
        modified.write_bytes(modified.read_bytes() + b"\n<!-- local edit -->\n")
        preserved_edit = modified.read_bytes()
        desired = source_names()
        for name in git(publisher, "ls-files", "-z").stdout.decode().split("\0"):
            if name and name not in desired:
                (publisher / name).unlink()
        for name in desired:
            destination = publisher / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)
        git(publisher, "add", "--all")
        git(publisher, "commit", "-m", "test: resource migration candidate")
        candidate = git(publisher, "rev-parse", "HEAD").stdout.decode().strip()
        git(publisher, "push", str(remote), "main")
        git(user, "pull", "--ff-only")
        assert git(user, "rev-parse", "HEAD").stdout.decode().strip() == candidate
        assert not (user / "webui/dist").exists()
        assert all((user / name).is_file() for name in desired)
        assert all((user / name).read_bytes() == contents for name, contents in personal.items())
        conflict = git(dirty, "pull", "--ff-only", check=False)
        assert conflict.returncode != 0 and modified.read_bytes() == preserved_edit
        updated = runtime_child(user, package)
        assert all((user / name).read_bytes() == contents for name, contents in personal.items())
        fresh = workspace / "fresh"
        git(workspace, "clone", str(remote), str(fresh))
        fresh_result = runtime_child(fresh, package, recover=True)
        nogit = workspace / "without-git"
        git(publisher, "archive", "--format=tar", "--output", str(workspace / "candidate.tar"), "HEAD")
        nogit.mkdir()
        with tarfile.open(workspace / "candidate.tar") as bundle:
            bundle.extractall(nogit, filter="data")
        archive_result = runtime_child(nogit, package)
        assert updated["source_commit"] != candidate
        print(
            json.dumps(
                {
                    "removed_files": len(old_files),
                    "personal_files_unchanged": True,
                    "dirty_checkout_preserved": True,
                    "updated": updated,
                    "fresh": fresh_result,
                    "without_git": archive_result,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
