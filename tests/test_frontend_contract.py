"""验证真实资源契约、路径限制与无需 Git 的构建标识。"""

import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from src.webui.frontend_contract import (
    FIXED_INPUTS,
    MANIFEST,
    build_identity,
    extract_archive,
    package_directory,
    validate_directory,
)

ROOT = Path(__file__).resolve().parents[1]


def source_copy(destination: Path) -> Path:
    """只复制构建所需输入，避免测试触及业务配置、数据或生产实例。"""
    for name in FIXED_INPUTS:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    for directory in ("src", "public"):
        (destination / "webui" / directory).mkdir(parents=True)
    (destination / "webui/src/example.ts").write_bytes(b"export const x = 1\n")
    return destination


def small_package(root: Path, output: Path) -> str:
    """用合法最小入口走真实打包逻辑，返回源码构建标识。"""
    dist = output / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        '<html><body><script src="/assets/main.js"></script></body></html>', encoding="utf-8"
    )
    (dist / "assets/main.js").write_text('document.body.dataset.ready = "yes"', encoding="utf-8")
    return package_directory(root, dist, output, "a" * 40)


class ContractTests(unittest.TestCase):
    """以实际文件验证完整性和跨平台构建输入，不只检查函数返回。"""

    def setUp(self):
        """建立每例独享的无 Git 源码目录。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.root = source_copy(self.work / "source")

    def test_identity_normalizes_text_only_and_ignores_backend(self):
        """文本换行与后端文件不改变标识；前端字节和工具链改变标识。"""
        original = build_identity(self.root)[0]
        path = self.root / "webui/src/example.ts"
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        (self.root / "backend.py").write_text("changed", encoding="utf-8")
        self.assertEqual(original, build_identity(self.root)[0])
        path.write_bytes(b"changed\n")
        self.assertNotEqual(original, build_identity(self.root)[0])
        original = build_identity(self.root)[0]
        (self.root / "webui/public/image.png").write_bytes(b"\r\n")
        self.assertNotEqual(original, build_identity(self.root)[0])
        original = build_identity(self.root)[0]
        (self.root / "webui/public/image.png").write_bytes(b"\n")
        self.assertNotEqual(original, build_identity(self.root)[0])

    def test_workflow_newlines_do_not_change_build_id(self):
        """Git 的 Windows 检出会转换工作流换行，资源标识必须与 Linux 一致。"""
        workflow = self.root / ".github/workflows/webui.yml"
        content = workflow.read_text(encoding="utf-8").encode("utf-8")
        original = build_identity(self.root)[0]
        for ending in (b"\r\n", b"\r", b"\n"):
            with self.subTest(ending=ending):
                workflow.write_bytes(content.replace(b"\n", ending))
                self.assertEqual(original, build_identity(self.root)[0])

    def test_roundtrip_and_corrupt_file(self):
        """包经实际解压与清单校验后可用，文件改动与错误标识均拒绝。"""
        output = self.work / "package"
        identity = small_package(self.root, output)
        target = self.work / "extracted"
        target.mkdir()
        extract_archive(output / "webui.zip", target, identity)
        with self.assertRaises(ValueError):
            validate_directory(target, "f" * 64)
        (target / "assets/main.js").write_text("tampered", encoding="utf-8")
        with self.assertRaises(ValueError):
            validate_directory(target, identity)

    def test_reproducible_pack_and_missing_entry(self):
        """相同输入和提交重复打包一致；缺失首页所需文件时失败。"""
        first, second = self.work / "one", self.work / "two"
        small_package(self.root, first)
        small_package(self.root, second)
        self.assertEqual((first / "webui.zip").read_bytes(), (second / "webui.zip").read_bytes())
        (second / "dist/assets/main.js").unlink()
        with self.assertRaises(ValueError):
            package_directory(self.root, second / "dist", second, "a" * 40)

    def test_hostile_archives(self):
        """逐项拒绝穿越、大小写重复、符号链接、设备名及非法路径。"""
        cases = [
            ("../escape", 0),
            ("/absolute", 0),
            ("C:/drive", 0),
            ("a\\b", 0),
            ("CON.txt", 0),
            ("link", 0o120777 << 16),
            ("a/../b", 0),
            ("a.", 0),
        ]
        for index, (name, mode) in enumerate(cases):
            with self.subTest(name=name):
                archive = self.work / f"bad{index}.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    entry = zipfile.ZipInfo(name)
                    # Windows 的 ZipInfo 构造器会规范化反斜杠；恢复原名才能模拟恶意归档。
                    entry.filename = name
                    entry.external_attr = mode
                    bundle.writestr(entry, b"bad")
                target = self.work / f"target{index}"
                target.mkdir()
                with self.assertRaises(ValueError):
                    extract_archive(archive, target, "a" * 64)
                self.assertEqual(list(target.iterdir()), [])
        archive = self.work / "duplicate.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("A.js", "one")
            bundle.writestr("a.js", "two")
        with self.assertRaises(ValueError):
            extract_archive(archive, self.work / "target0", "a" * 64)

    def test_limits_and_manifest_inventory(self):
        """限制压缩大小、展开大小、文件数，拒绝无清单文件。"""
        output = self.work / "package"
        identity = small_package(self.root, output)
        target = self.work / "extract"
        target.mkdir()
        for limit in ("MAX_ARCHIVE_BYTES", "MAX_EXPANDED_BYTES", "MAX_FILES"):
            with patch(f"src.webui.frontend_contract.{limit}", 1), self.assertRaises(ValueError):
                extract_archive(output / "webui.zip", target, identity)
        extract_archive(output / "webui.zip", target, identity)
        metadata = json.loads((target / MANIFEST).read_text(encoding="utf-8"))
        self.assertIn("webui/bun.lock", metadata["inputs"])
        (target / "extra.js").touch()
        with self.assertRaises(ValueError):
            validate_directory(target, identity)

    def test_unsupported_compression_is_rejected_before_extract(self):
        """不接受构建器不会生成的压缩格式，避免解包异常使恢复状态悬空。"""
        archive = self.work / "unsupported.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_BZIP2) as bundle:
            bundle.writestr("main.js", "example")
        target = self.work / "unsupported"
        target.mkdir()
        with self.assertRaises(ValueError):
            extract_archive(archive, target, "a" * 64)
        self.assertEqual(list(target.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
