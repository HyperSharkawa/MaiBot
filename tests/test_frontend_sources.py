"""验证镜像配置只读、候选排序和低速统计边界。"""

import json
import logging
import tempfile
import unittest
from pathlib import Path

from src.webui.frontend_sources import GITHUB, normalize_prefix, read_sources
from src.webui.frontend_transfer import TransferMeter


class SourcesTests(unittest.TestCase):
    """所有配置均位于本例临时目录，文件内容用于检查没有被改写。"""

    def setUp(self):
        """创建独立测试目录和无业务依赖的日志对象。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "data/webui.json"
        self.logger = logging.getLogger("test.frontend.sources")

    def write_mirrors(self, mirrors):
        """只向测试目录写入虚构 Token 及镜像列表。"""
        self.config.parent.mkdir(exist_ok=True)
        self.config.write_text(json.dumps({"access_token": "synthetic", "git_mirrors": mirrors}), encoding="utf-8")

    def test_missing_and_malformed_config_remain_unchanged(self):
        """缺配置不创建文件，坏配置只提示而不覆盖。"""
        self.assertEqual(
            [source.id for source in read_sources(self.root, self.logger)], ["github", "edgeone", "hongkong", "ghproxy"]
        )
        self.assertFalse(self.config.parent.exists())
        self.write_mirrors([])
        for contents in ("{broken", "[]", '{"git_mirrors": 2}'):
            self.config.write_text(contents, encoding="utf-8")
            with self.assertLogs(self.logger, "WARNING"):
                self.assertEqual(len(read_sources(self.root, self.logger)), 4)
            self.assertEqual(self.config.read_text(encoding="utf-8"), contents)

    def test_priorities_deduplication_and_disabled_defaults(self):
        """相同前缀规范化去重，禁用优先于重复启用且不会被默认源补回。"""
        self.write_mirrors(
            [
                {"clone_prefix": "https://slow.example/https://github.com", "enabled": True, "priority": 20},
                {"clone_prefix": "https://FAST.example:443/https://github.com/", "enabled": True, "priority": 1},
                {"clone_prefix": "https://fast.example/https://github.com", "enabled": True, "priority": 3},
                {"clone_prefix": GITHUB + "/", "enabled": False},
                {"clone_prefix": "https://edgeone.gh-proxy.com/https://github.com/", "enabled": False},
            ]
        )
        before = self.config.read_bytes()
        sources = read_sources(self.root, self.logger)
        self.assertEqual(
            [source.name for source in sources], ["自定义加速源 2", "自定义加速源 1", "香港加速", "gh-proxy 加速"]
        )
        self.assertEqual(sources[0].prefix, "https://fast.example/https://github.com")
        self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(
            sources[0].base_url("a" * 64).endswith("HyperSharkawa/MaiBot/releases/download/webui-" + "a" * 64)
        )

    def test_invalid_entries_are_not_logged_or_rewritten(self):
        """不适用地址逐条跳过，日志不包含地址中可能出现的凭据。"""
        invalid = [
            "git@github.com:test",
            "/tmp/source",
            "http://proxy.example",
            "https://u:secret@example.com",
            "https://example.com?secret=1",
            "https://example.com#secret",
            "https://example.com:bad",
            "https://example.com/ space",
        ]
        self.write_mirrors(
            [{"enabled": True, "clone_prefix": prefix} for prefix in invalid] + [None, {"enabled": "yes"}]
        )
        before = self.config.read_bytes()
        with self.assertLogs(self.logger, "WARNING") as logs:
            sources = read_sources(self.root, self.logger)
        self.assertEqual(len(sources), 4)
        self.assertNotIn("secret", "".join(logs.output))
        self.assertEqual(self.config.read_bytes(), before)
        for prefix in invalid:
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                normalize_prefix(prefix)

    def test_org_domains_are_not_rewritten(self):
        """历史节点地址原样保留，与不同域名的内置节点分别处理。"""
        self.write_mirrors([{"clone_prefix": "https://gh-proxy.org/https://github.com", "enabled": True}])
        self.assertEqual(read_sources(self.root, self.logger)[1].prefix, "https://gh-proxy.org/https://github.com")


class MeterTests(unittest.TestCase):
    """使用受控单调时间检查窗口，不等待真实的一分钟。"""

    def test_exact_threshold_and_bounded_window(self):
        """恰好达到 5 KB/s 可持续运行，采样数量不会随时间增长。"""
        meter = TransferMeter(0)
        for second in range(1, 10001):
            self.assertEqual(meter.sample(second, second * 5000), (5000, 0))
            self.assertLessEqual(len(meter.samples), 12)

    def test_sixty_seconds_low_and_recovery(self):
        """连续低速精确累计，恢复后归零，再次低速重新计时。"""
        meter = TransferMeter(0)
        for second in range(1, 61):
            self.assertEqual(meter.sample(second, second * 4999), (4999, second))
        speed, low = meter.sample(61, 60 * 4999 + 60000)
        self.assertGreater(speed, 5000)
        self.assertEqual(low, 0)
        for second in range(62, 82):
            speed, low = meter.sample(second, 60 * 4999 + 60000)
        self.assertEqual(speed, 0)
        self.assertLess(low, 60)

    def test_no_data_and_initial_short_window(self):
        """首个窗口按实际时间计算，没有字节也能累计低速。"""
        meter = TransferMeter(10)
        self.assertEqual(meter.sample(10.5, 2500), (5000, 0))
        meter = TransferMeter(0)
        for second in range(1, 61):
            speed, low = meter.sample(second, 0)
        self.assertEqual((speed, low), (0, 60))
