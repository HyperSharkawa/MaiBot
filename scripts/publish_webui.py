"""CI 专用发布入口：先校验，再创建草稿并上传，最后公开；不覆盖已有资源。"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.webui.frontend_contract import (  # noqa: E402
    REPOSITORY,
    build_identity,
    digest,
    extract_archive,
)


def verify_package(directory: Path, identity: str) -> dict:
    """校验本地或已经发布的完整资源，拒绝只有名字相同的冲突包。"""
    archive = directory / "webui.zip"
    check = (directory / "webui.zip.sha256").read_text(encoding="ascii").split()
    if check != [digest(archive.read_bytes()), "webui.zip"]:
        raise ValueError("发布包完整性校验失败")
    with tempfile.TemporaryDirectory() as temporary:
        manifest = extract_archive(archive, Path(temporary), identity)
    _, toolchain, inputs = build_identity(ROOT)
    if manifest["inputs"] != inputs or manifest["toolchain"] != toolchain:
        raise ValueError("已有资源与当前构建契约冲突，禁止覆盖")
    return manifest


def api(path: str, body: dict | None = None) -> dict | None:
    """通过 CI 注入的令牌访问固定仓库，仅将 404 解释为不存在。"""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and body is None:
            return None
        raise RuntimeError(f"GitHub API 请求失败：HTTP {error.code}") from None


def main() -> None:
    """仅显式 publish 命令执行远程写入；identity 只输出 CI 变量。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("identity", "publish"))
    args = parser.parse_args()
    identity, _, _ = build_identity(ROOT)
    if args.command == "identity":
        print(identity)
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a", encoding="utf-8") as stream:
                stream.write(f"build_id={identity}\n")
        return
    package = ROOT / "webui/.build"
    manifest = verify_package(package, identity)
    tag = f"webui-{identity}"
    existing = api(f"releases/tags/{tag}")
    if existing is not None:
        names = {asset["name"] for asset in existing["assets"]}
        if names != {"webui.zip", "webui.zip.sha256"}:
            raise SystemExit("已有 Release 不完整或存在冲突，请人工检查草稿；禁止覆盖。")
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(
                ["gh", "release", "download", tag, "--repo", REPOSITORY, "--dir", temporary, "--pattern", "webui.zip*"],
                check=True,
            )
            verify_package(Path(temporary), identity)
        if not existing["draft"]:
            print(f"已存在有效资源：{tag}")
            return
    else:
        api(
            "releases",
            {
                "tag_name": tag,
                "target_commitish": manifest["source_commit"],
                "name": f"WebUI {identity[:12]}",
                "body": "此发行版仅供 MaiBot 自动获取前端资源。",
                "draft": True,
                "make_latest": "false",
            },
        )
        subprocess.run(
            [
                "gh",
                "release",
                "upload",
                tag,
                str(package / "webui.zip"),
                str(package / "webui.zip.sha256"),
                "--repo",
                REPOSITORY,
            ],
            check=True,
        )
        # 上传后再下载验证，完整草稿才允许公开给运行时。
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(
                ["gh", "release", "download", tag, "--repo", REPOSITORY, "--dir", temporary, "--pattern", "webui.zip*"],
                check=True,
            )
            verify_package(Path(temporary), identity)
    subprocess.run(["gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest=false"], check=True)


if __name__ == "__main__":
    main()
