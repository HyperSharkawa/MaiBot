"""模拟 GitHub 边界验证不可变发行，所有资源校验仍使用真实文件。"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import publish_webui
from test_frontend_contract import small_package, source_copy


class PublishTests(unittest.TestCase):
    """只替换远程 API 和 gh 命令，不实际发布或上传。"""

    def setUp(self):
        """准备可被发布器完整校验的临时包。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = source_copy(Path(self.temporary.name) / "source")
        self.package = self.root / "webui/.build"
        small_package(self.root, self.package)
        self.commands = []
        self.enterContext(patch.object(publish_webui, "ROOT", self.root))
        self.enterContext(patch("sys.argv", ["publish_webui.py", "publish"]))
        self.enterContext(patch.object(publish_webui.subprocess, "run", side_effect=self.run_gh))

    def run_gh(self, command, **kwargs):
        """以本地文件模拟下载，记录写入顺序以验证草稿公开边界。"""
        self.commands.append(command)
        if command[2] == "download":
            target = Path(command[command.index("--dir") + 1])
            for name in ("webui.zip", "webui.zip.sha256"):
                shutil.copyfile(self.package / name, target / name)

    def test_valid_existing_release_is_reused(self):
        """已有正确公开资源只下载校验，不覆盖、不重新公开。"""
        release = {"draft": False, "assets": [{"name": name} for name in ("webui.zip", "webui.zip.sha256")]}
        with patch.object(publish_webui, "api", return_value=release):
            publish_webui.main()
        self.assertEqual([command[2] for command in self.commands], ["download"])

    def test_incomplete_existing_release_is_not_overwritten(self):
        """不完整草稿要求人工处理，禁止覆盖资源。"""
        with (
            patch.object(publish_webui, "api", return_value={"draft": True, "assets": []}),
            self.assertRaises(SystemExit),
        ):
            publish_webui.main()
        self.assertEqual(self.commands, [])

    def test_draft_upload_verified_before_publish(self):
        """新包按创建草稿、上传、下载校验、公开顺序发布。"""
        with patch.object(publish_webui, "api", side_effect=[None, {}]) as api:
            publish_webui.main()
        self.assertTrue(api.call_args_list[1].args[1]["draft"])
        self.assertEqual(api.call_args_list[1].args[1]["make_latest"], "false")
        self.assertEqual([command[2] for command in self.commands], ["upload", "download", "edit"])
        self.assertIn("--latest=false", self.commands[-1])


if __name__ == "__main__":
    unittest.main()
