"""前端构建与运行时共用的纯文件契约；导入时不读取配置、不连接数据库。"""

import hashlib
import json
import re
import stat
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

CONTRACT_VERSION = 1
REPOSITORY = "HyperSharkawa/MaiBot"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 256 * 1024 * 1024
MAX_FILES = 10_000
MANIFEST = "webui-manifest.json"
BUILD_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
FIXED_INPUTS = (
    "webui/index.html",
    "webui/package.json",
    "webui/bun.lock",
    "webui/toolchain.json",
    "webui/.node-version",
    "webui/vite.config.ts",
    "webui/tsconfig.json",
    "webui/tsconfig.app.json",
    "webui/tsconfig.node.json",
    "webui/tailwind.config.js",
    "webui/postcss.config.js",
    "webui/LICENSE",
    "webui/UPSTREAM.md",
    "scripts/build_webui.py",
    "src/webui/frontend_contract.py",
    ".github/workflows/webui.yml",
)
TEXT_SUFFIXES = {
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".css",
    ".html",
    ".svg",
    ".txt",
    ".md",
    ".py",
    ".lock",
    ".yml",
    ".yaml",
}


def json_bytes(value: object) -> bytes:
    """生成不受平台缩进和换行影响的确定性 JSON。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(data: bytes) -> str:
    """计算构建标识或发行文件完整性所用的 SHA-256。"""
    return hashlib.sha256(data).hexdigest()


def build_identity(root: Path) -> tuple[str, dict, dict[str, str]]:
    """读取明确的构建输入，返回构建标识、工具链和输入清单；无需 Git。"""
    paths = {root / name for name in FIXED_INPUTS}
    for directory in ("src", "public"):
        paths.update(path for path in (root / "webui" / directory).rglob("*") if path.is_file())
    inventory = {}
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError(f"构建输入不允许符号链接：{path.name}")
        data = path.read_bytes()
        if path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".node-version"}:
            data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        inventory[path.relative_to(root).as_posix()] = digest(data)
    toolchain = json.loads((root / "webui/toolchain.json").read_text(encoding="utf-8"))
    identity = digest(json_bytes({"contract": CONTRACT_VERSION, "inputs": inventory}))
    return identity, toolchain, inventory


def safe_name(name: str) -> str:
    """校验包内相对路径，统一拒绝跨平台路径穿越、设备名和别名。"""
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or path.as_posix() != name:
        raise ValueError("前端包包含非法路径")
    for part in path.parts:
        if part in {".", ".."} or ":" in part or part.endswith((".", " ")):
            raise ValueError("前端包包含非法路径")
        if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", part.split(".")[0]):
            raise ValueError("前端包包含保留路径")
    return name


class _EntryParser(HTMLParser):
    """仅提取首页直接依赖的本地资源，用于防止缺入口资源的包被启用。"""

    def __init__(self):
        super().__init__()
        self.resources: set[str] = set()
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        """收集脚本和样式等入口资源；发行包不接受外部入口脚本。"""
        values = dict(attrs)
        if tag == "script" and values.get("src"):
            value = values["src"]
            self.scripts += 1
        elif tag == "link" and values.get("href"):
            value = values["href"]
        else:
            return
        url = urlsplit(value)
        if url.scheme or url.netloc:
            raise ValueError("前端入口不能引用外部资源")
        self.resources.add(safe_name(unquote(url.path).lstrip("/")))


def validate_entry(root: Path) -> None:
    """确认首页和直接引用资源均存在；适用于旧版没有清单的本地资源。"""
    parser = _EntryParser()
    parser.feed((root / "index.html").read_text(encoding="utf-8"))
    if not parser.scripts:
        raise ValueError("前端首页缺少入口脚本")
    for name in parser.resources:
        path = root / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("前端入口资源不完整")


def validate_directory(root: Path, expected_id: str | None = None) -> dict:
    """校验已展开资源的清单和每个文件，失败时不修改任何文件。"""
    manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("前端清单必须为对象")
    if manifest.get("contract") != CONTRACT_VERSION:
        raise ValueError("不支持的前端包契约")
    identity = manifest.get("build_id", "")
    if not BUILD_ID_PATTERN.fullmatch(identity) or (expected_id is not None and identity != expected_id):
        raise ValueError("前端包版本不匹配")
    if digest(json_bytes({"contract": CONTRACT_VERSION, "inputs": manifest.get("inputs")})) != identity:
        raise ValueError("前端包输入清单与构建标识不一致")
    files = manifest.get("files")
    if not isinstance(files, dict) or not {"index.html", "LICENSE", "UPSTREAM.md"}.issubset(files):
        raise ValueError("前端包清单不完整")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if actual != set(files) | {MANIFEST}:
        raise ValueError("前端包文件与清单不一致")
    total = 0
    if len(actual) > MAX_FILES:
        raise ValueError("前端包文件过多")
    for name, info in files.items():
        path = root / safe_name(name)
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("前端包不允许符号链接")
        total += path.stat().st_size
        if total > MAX_EXPANDED_BYTES or path.stat().st_size != info["size"]:
            raise ValueError("前端包大小不符或超限")
        if digest(path.read_bytes()) != info["sha256"]:
            raise ValueError("前端文件校验失败")
    validate_entry(root)
    return manifest


def extract_archive(archive: Path, destination: Path, expected_id: str) -> dict:
    """将已下载 ZIP 解压到空的暂存目录，验证后才允许调用方激活。"""
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("前端下载包超过大小限制")
    if any(destination.iterdir()):
        raise ValueError("前端解压目录必须为空")
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > MAX_FILES or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
            raise ValueError("前端解压包超过大小限制")
        names = set()
        for item in members:
            name = safe_name(item.filename)
            mode = stat.S_IFMT(item.external_attr >> 16)
            if (
                name.casefold() in names
                or item.is_dir()
                or mode not in (0, stat.S_IFREG)
                or item.orig_filename != item.filename
            ):
                raise ValueError("前端包包含重复路径或特殊文件")
            names.add(name.casefold())
            if (
                item.flag_bits & 1
                or name.endswith(".map")
                or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            ):
                raise ValueError("前端包包含加密文件、源码映射或不支持的压缩格式")
        for item in members:
            target = destination / item.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, target.open("xb") as output:
                while chunk := source.read(256 * 1024):
                    output.write(chunk)
    return validate_directory(destination, expected_id)


def package_directory(root: Path, dist: Path, output: Path, source_commit: str) -> str:
    """给构建目录写入清单与许可证，并输出可重复打包的 ZIP 及校验文件。"""
    identity, toolchain, inputs = build_identity(root)
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError("必须提供真实的完整源码提交 ID")
    for name in ("LICENSE", "UPSTREAM.md"):
        (dist / name).write_bytes((root / "webui" / name).read_bytes().replace(b"\r\n", b"\n"))
    files = {}
    for path in sorted(dist.rglob("*")):
        if path.is_file() and path.name != MANIFEST:
            name = safe_name(path.relative_to(dist).as_posix())
            if path.is_symlink() or path.suffix == ".map":
                raise ValueError("构建产物包含符号链接或源码映射")
            data = path.read_bytes()
            files[name] = {"size": len(data), "sha256": digest(data)}
    manifest = {
        "contract": CONTRACT_VERSION,
        "build_id": identity,
        "source_commit": source_commit,
        "toolchain": toolchain,
        "inputs": inputs,
        "files": files,
    }
    (dist / MANIFEST).write_bytes(json_bytes(manifest))
    validate_directory(dist, identity)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "webui.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in sorted(set(files) | {MANIFEST}):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, (dist / name).read_bytes())
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("前端下载包超过大小限制")
    (output / "webui.zip.sha256").write_text(f"{digest(archive.read_bytes())}  webui.zip\n", encoding="ascii")
    return identity
