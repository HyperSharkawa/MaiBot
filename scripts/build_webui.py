"""使用锁定工具链构建前端；只写入指定输出目录，不覆盖现有页面。"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.webui.frontend_contract import build_identity, package_directory  # noqa: E402


def main() -> None:
    """输出构建标识，或执行冻结安装、类型检查、构建和打包。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("identity", "build"))
    parser.add_argument("--output", type=Path, default=ROOT / "webui/.build")
    parser.add_argument("--bun", default="bun")
    parser.add_argument("--source-commit")
    args = parser.parse_args()
    identity, toolchain, _ = build_identity(ROOT)
    if args.command == "identity":
        print(json.dumps({"build_id": identity, "tag": f"webui-{identity}", **toolchain}))
        return
    bun = shutil.which(args.bun)
    node = shutil.which("node")
    if not bun or not node:
        raise SystemExit("开发构建需要 Node 和 Bun；运行 Bot 不需要安装它们。")
    for executable, version in ((bun, toolchain["bun"]), (node, toolchain["node"])):
        actual = subprocess.check_output([executable, "--version"], text=True).strip().removeprefix("v")
        if actual != version:
            raise SystemExit(f"工具链版本不符：{executable} 需要 {version}，当前 {actual}")
    source_commit = (
        args.source_commit or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )
    output = args.output.resolve()
    if output == (ROOT / "webui/dist").resolve() or (ROOT / "webui/dist").resolve().is_relative_to(output):
        raise SystemExit("输出目录不能覆盖现有 webui/dist")
    dist = output / "dist"
    subprocess.run([bun, "install", "--frozen-lockfile", "--ignore-scripts"], cwd=ROOT / "webui", check=True)
    subprocess.run([bun, "run", "build", "--outDir", str(dist)], cwd=ROOT / "webui", check=True)
    print(package_directory(ROOT, dist, output, source_commit))


if __name__ == "__main__":
    main()
