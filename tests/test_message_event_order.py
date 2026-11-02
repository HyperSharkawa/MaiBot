"""用实际接收、事件和存储代码验证识图等待边界，所有数据位于隔离副本。"""

import asyncio
import base64
import importlib
import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
IS_CHILD = "--isolated-source" in sys.argv
CASE = sys.argv[sys.argv.index("--case") + 1] if "--case" in sys.argv else None


class IsolatedEventTests(unittest.TestCase):
    """复制真实源码并启动独立子进程，避免接触工作区配置及数据库。"""

    def test_isolated_contracts(self):
        """当前解释器运行隔离用例；缺依赖或跳过用例均不能算通过。"""
        with tempfile.TemporaryDirectory(prefix="maibot-events-") as directory:
            source = Path(directory) / "source"
            for name in ("src", "template"):
                shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
            (source / "config").mkdir()
            for name in ("bot_config", "model_config"):
                shutil.copyfile(source / "template" / f"{name}_template.toml", source / "config" / f"{name}.toml")
            args = [sys.executable, str(Path(__file__).resolve()), "--isolated-source", str(source)]
            if CASE:
                args.extend(("--case", CASE))
            subprocess.run(
                args,
                cwd=directory,
                env={**os.environ, "PYTHONNOUSERSITE": "1", "PYTHON_LAZY_IMPORTS": "normal"},
                check=True,
                timeout=120,
            )
            self.assertTrue((source / "events-tests.completed").exists())


class MessageEventTests(unittest.IsolatedAsyncioTestCase):
    """用显式事件控制识图完成，保留实际消息转换、分发、命令和 SQLite 存储。"""

    @classmethod
    def setUpClass(cls):
        """只在专用临时副本导入宿主，拒绝对外连接并保留 Windows 回环能力。"""
        source = Path(sys.argv[sys.argv.index("--isolated-source") + 1]).resolve()
        if not source.parent.name.startswith("maibot-events-"):
            raise RuntimeError("测试必须在专用隔离副本运行")
        os.chdir(source)
        sys.path.insert(0, str(source))
        original_connect = socket.socket.connect

        def connect(sock, address):
            """仅允许事件循环自身的回环连接，不连接模型或消息平台。"""
            if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1", "localhost"):
                raise AssertionError("测试禁止外部网络连接")
            return original_connect(sock, address)

        guard = patch.object(socket.socket, "connect", new=connect)
        guard.start()
        cls.addClassCleanup(guard.stop)
        importlib.import_module("src.main")
        cls.bot_module = importlib.import_module("src.chat.message_receive.bot")

    async def asyncSetUp(self):
        """隔离注册表并安装可控的外部识图与发送边界，不替换消息处理流程。"""
        from src.common.database.database_model import Messages, Images, ImageDescriptions
        from src.plugin_system import BaseEventHandler, EventType
        from src.plugin_system import core
        from src.plugin_system.core.events_manager import EventsManager
        from src.plugin_system.core.component_registry import ComponentRegistry
        from src.chat.heart_flow.heartflow import heartflow
        from src.chat.utils.utils_image import get_image_manager
        from src.chat.message_receive.chat_stream import get_chat_manager

        self.Messages = Messages
        Messages.delete().execute()
        Images.delete().execute()
        ImageDescriptions.delete().execute()
        self.chat_manager = get_chat_manager()
        self.chat_manager.streams.clear()
        self.chat_manager.last_messages.clear()
        self.events = EventsManager()
        self.registry = ComponentRegistry()
        self.seen = {}
        self.received = []
        self.published = []
        self.sent = []
        self.visuals = {}
        self.tasks = []
        self.blocked = set()
        self.replacements = {}
        self.replies = set()
        self.commands = {}
        self.bot = self.bot_module.ChatBot()
        config = self.bot_module.global_config
        self.enterContext(patch.object(config.chat, "wait_for_image_recognition", True))
        self.enterContext(patch.object(config.bot, "qq_account", "9000"))
        self.enterContext(patch.object(config.bot, "platforms", []))
        self.enterContext(patch.object(config.message_receive, "ban_words", []))
        self.enterContext(patch.object(config.message_receive, "ban_msgs_regex", []))
        self.enterContext(patch.object(self.bot_module, "events_manager", self.events))
        self.enterContext(patch.object(core, "events_manager", self.events))
        self.enterContext(patch.object(self.bot_module, "component_registry", self.registry))
        self.enterContext(patch.object(core, "component_registry", self.registry))
        events_module = importlib.import_module("src.plugin_system.core.events_manager")
        self.enterContext(patch.object(events_module, "events_manager", self.events))
        self.enterContext(patch.object(get_image_manager(), "_image_description", new=self.recognize))
        sender = importlib.import_module("src.chat.message_receive.uni_message_sender")
        self.enterContext(patch.object(sender, "_send_message", new=self.send))
        self.enterContext(patch.object(heartflow, "get_or_create_heartflow_chat", new=self.notify_chat))
        owner = self

        class EditingHandler(BaseEventHandler):
            """模拟同步过滤与文本修改，保持真实事件管理器处理返回值。"""

            event_type = EventType.ON_MESSAGE
            handler_name = "event_order_edit"
            intercept_message = True
            weight = 10

            async def execute(self, message):
                """按本例设置修改或拦截消息，不直接写入数据库。"""
                label = message.raw_message
                if label in owner.replacements:
                    message.modify_plain_text(owner.replacements[label])
                return True, label not in owner.blocked, None, None, message

        class ObservingHandler(BaseEventHandler):
            """模拟即时回复插件，以真实异步事件任务记录执行并调用发送接口。"""

            event_type = EventType.ON_MESSAGE
            handler_name = "event_order_observe"

            async def execute(self, message):
                """发送指定的合成回复后通知测试，不改变消息的继续处理结果。"""
                label = message.raw_message
                owner.received.append((label, message.stream_id))
                if label in owner.replies:
                    owner.assertTrue(await self.send_text(message.stream_id, "synthetic reply"))
                owner.signal(label).set()
                return True, True, None, None, None

        for handler in (EditingHandler, ObservingHandler):
            self.assertTrue(self.events.register_event_subscriber(handler.get_handler_info(), handler))

    async def asyncTearDown(self):
        """取消本例接收与事件任务，确保失败断言也不会留下在途消息。"""
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for name in list(self.events._handler_mapping):
            await self.events.unregister_event_subscriber(name)
        self.assertEqual(self.bot.incoming_order.active_receipts, 0)
        self.assertEqual(self.bot.incoming_order.active_sessions, 0)

    def signal(self, label):
        """获取本例某条消息已执行事件的通知，不依赖固定等待时间。"""
        return self.seen.setdefault(label, asyncio.Event())

    async def wait(self, event):
        """超时只用于报告失败，成功条件由生产路径触发的事件决定。"""
        await asyncio.wait_for(event.wait(), 3)

    async def recognize(self, image_hash, data):
        """替代外部 VLM 响应；控制收到请求、完成和失败三个时刻。"""
        started, release, failed = self.visuals[data]
        started.set()
        await release.wait()
        if failed:
            raise RuntimeError("synthetic recognition failure")
        return "合成测试图片"

    async def send(self, message, show_log=True):
        """只替代发往适配器的边界，后续发送事件和存储仍使用真实实现。"""
        self.sent.append(message.processed_plain_text)
        return True

    async def notify_chat(self, stream_id, stored_id):
        """记录真实入库后的聊天通知，不在单元测试中启动模型循环。"""
        self.published.append((stream_id, stored_id))
        return object()

    def start(self, label, *, visual=False, group="9200", text=None, failed=False):
        """向真实接收入口投递合成消息；图片返回可控制的识图通知及放行事件。"""
        from PIL import Image

        segment = {"type": "text", "data": text or label}
        started, release = asyncio.Event(), asyncio.Event()
        if visual:
            output = io.BytesIO()
            Image.new("RGB", (8, 8), (len(self.visuals) + 1, 40, 80)).save(output, format="PNG")
            data = base64.b64encode(output.getvalue()).decode()
            self.visuals[data] = (started, release, failed)
            segment = {"type": "image", "data": data}
            if text:
                segment = {"type": "seglist", "data": [{"type": "text", "data": text}, segment]}
        info = {
            "platform": "qq",
            "message_id": label,
            "time": 100.0,
            "user_info": {"platform": "qq", "user_id": "9100", "user_nickname": "合成用户"},
        }
        if group:
            info["group_info"] = {"platform": "qq", "group_id": group, "group_name": "合成群"}
        task = asyncio.create_task(
            self.bot.message_process(
                {
                    "message_info": info,
                    "message_segment": segment,
                    "raw_message": label,
                }
            )
        )
        self.tasks.append(task)
        return task, started, release

    def stored(self, label):
        """查询本例真实消息行，未存储时返回空值。"""
        return self.Messages.get_or_none(self.Messages.message_id == label)

    async def test_text_event_and_reply_before_image_publication(self):
        """前图未完成时插件已回复，但原始文字仍未入库且未成为共享最近消息。"""
        image, started, release = self.start("image", visual=True)
        await self.wait(started)
        self.replies.add("keyword")
        text, _, _ = self.start("keyword")
        await self.wait(self.signal("keyword"))
        self.assertEqual(self.sent, ["synthetic reply"])
        self.assertFalse(text.done())
        self.assertIsNone(self.stored("keyword"))
        self.assertEqual(self.published, [])
        self.assertEqual(self.chat_manager.last_messages, {})
        stream_id = self.received[0][1]
        self.assertIsNotNone(self.chat_manager.get_stream(stream_id))
        from src.common.message_repository import has_unread_messages, read_chat_batch
        from src.chat.round_context import ChatReadState

        self.assertFalse(has_unread_messages(stream_id, 0, "9000"))
        release.set()
        await asyncio.gather(image, text)
        await self.wait(self.signal("image"))
        rows = list(self.Messages.select().order_by(self.Messages.id))
        self.assertEqual(rows[0].user_id, "9000")
        self.assertEqual(rows[0].processed_plain_text, "synthetic reply")
        self.assertEqual([row.message_id for row in rows[1:]], ["image", "keyword"])
        self.assertEqual([label for label, _ in self.received].count("keyword"), 1)
        batch = read_chat_batch(stream_id, 0, 10, 18, "9000")
        self.assertEqual([row.message_id for row in batch.unread], ["image", "keyword"])
        self.assertIn("synthetic reply", [row.processed_plain_text for row in batch.messages])
        state = ChatReadState(stream_id, self.stored("image").id, "qq")
        self.assertTrue(state.admit(state.capture(18, None)))
        self.assertFalse(state.has_unread())
        later, _, _ = self.start("later")
        await later
        self.assertTrue(state.has_unread())

    async def test_later_image_event_does_not_publish_early(self):
        """后图可先分发事件，原图完成前两图和后续文字都不能进入普通聊天。"""
        first, started, release = self.start("first", visual=True)
        await self.wait(started)
        second, second_started, second_release = self.start("second", visual=True)
        await self.wait(second_started)
        second_release.set()
        await self.wait(self.signal("second"))
        text, _, _ = self.start("text")
        await self.wait(self.signal("text"))
        self.assertEqual(self.Messages.select().count(), 0)
        release.set()
        await asyncio.gather(first, second, text)
        self.assertEqual(
            [row.message_id for row in self.Messages.select().order_by(self.Messages.id)],
            [
                "first",
                "second",
                "text",
            ],
        )

    async def test_editing_handler_changes_stored_text(self):
        """同步事件的文本修改在等待之后仍被存储和普通聊天使用。"""
        image, started, release = self.start("image", visual=True)
        await self.wait(started)
        self.replacements["text"] = "修改后的合成内容"
        text, _, _ = self.start("text")
        await self.wait(self.signal("text"))
        self.assertIsNone(self.stored("text"))
        release.set()
        await asyncio.gather(image, text)
        self.assertEqual(self.stored("text").processed_plain_text, "修改后的合成内容")
        self.assertEqual(next(iter(self.chat_manager.last_messages.values())).processed_plain_text, "修改后的合成内容")

    async def test_interception_releases_receipts(self):
        """同步事件可先拦截文字；图片被拦截后解除后续消息的发布等待。"""
        self.blocked.update(("image", "blocked"))
        image, started, release = self.start("image", visual=True)
        await self.wait(started)
        blocked, _, _ = self.start("blocked")
        await asyncio.wait_for(blocked, 3)
        self.assertIsNone(self.stored("blocked"))
        self.assertEqual(self.bot.incoming_order.active_receipts, 1)
        text, _, _ = self.start("text")
        await self.wait(self.signal("text"))
        release.set()
        await asyncio.gather(image, text)
        self.assertIsNone(self.stored("image"))
        self.assertIsNotNone(self.stored("text"))

    async def test_commands_keep_storage_and_visibility_boundary(self):
        """两个命令等级都立即执行，存储仍等待图片且不分发消息事件。"""
        from src.plugin_system import BaseCommand
        from src.plugin_system.base.component_types import CommandInfo, ComponentType
        from src.common.message_repository import read_chat_batch

        owner = self

        class Command(BaseCommand):
            """使用真实命令注册和解析，仅返回测试指定的拦截等级。"""

            async def execute(self):
                """通知命令已经执行，不发送消息或提前存储输入。"""
                level = int(self.matched_groups["level"])
                owner.commands[level].set()
                return True, "synthetic command", level

        self.assertTrue(
            self.registry.register_component(
                CommandInfo(
                    name="order_command",
                    component_type=ComponentType.COMMAND,
                    command_pattern=r"^/stop(?P<level>[12])$",
                ),
                Command,
            )
        )
        image, started, release = self.start("image", visual=True)
        await self.wait(started)
        command_tasks = []
        for level in (1, 2):
            self.commands[level] = asyncio.Event()
            command, _, _ = self.start(f"command{level}", text=f"/stop{level}")
            command_tasks.append(command)
            await self.wait(self.commands[level])
            self.assertIsNone(self.stored(f"command{level}"))
        release.set()
        await asyncio.gather(image, *command_tasks)
        await self.wait(self.signal("image"))
        self.assertEqual([label for label, _ in self.received], ["image"])
        batch = read_chat_batch(self.stored("image").chat_id, 0, 10, 18, "9000")
        self.assertEqual([row.message_id for row in batch.unread], ["image"])
        self.assertEqual({row.message_id for row in batch.messages}, {"image", "command1"})
        self.assertEqual(self.stored("command2").intercept_message_level, 2)

    async def test_failed_recognition_releases_later_text(self):
        """识图失败沿用原有失败内容，后续事件已执行且最终仍能入库。"""
        image, started, release = self.start("image", visual=True, failed=True)
        await self.wait(started)
        text, _, _ = self.start("text")
        await self.wait(self.signal("text"))
        release.set()
        await asyncio.gather(image, text)
        self.assertIsNotNone(self.stored("text"))

    async def test_cancellation_cleans_image_and_waiter(self):
        """取消已分发事件的等待文字及识图任务后，不残留占位或重复事件。"""
        image, started, _ = self.start("image", visual=True)
        await self.wait(started)
        cancelled, _, _ = self.start("cancelled")
        await self.wait(self.signal("cancelled"))
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        text, _, _ = self.start("text")
        await self.wait(self.signal("text"))
        image.cancel()
        await asyncio.gather(image, return_exceptions=True)
        await text
        self.assertIsNone(self.stored("cancelled"))
        self.assertIsNotNone(self.stored("text"))
        self.assertEqual([label for label, _ in self.received].count("cancelled"), 1)

    async def test_other_session_and_private_message(self):
        """群聊识图不阻塞私聊；私聊自身也能提前分发文字事件。"""
        image, started, release = self.start("group-image", visual=True)
        await self.wait(started)
        private, _, _ = self.start("private", group=None)
        await asyncio.wait_for(private, 3)
        self.assertIsNotNone(self.stored("private"))
        other_image, other_started, other_release = self.start("private-image", visual=True, group=None)
        await self.wait(other_started)
        text, _, _ = self.start("private-text", group=None)
        await self.wait(self.signal("private-text"))
        self.assertIsNone(self.stored("private-text"))
        other_release.set()
        await asyncio.gather(other_image, text)
        self.assertFalse(image.done())
        release.set()
        await image

    async def test_disabled_wait_keeps_immediate_publication(self):
        """关闭等待时后续文字直接入库，不新增接收顺序状态。"""
        self.bot_module.global_config.chat.wait_for_image_recognition = False
        image, started, release = self.start("image", visual=True)
        await self.wait(started)
        text, _, _ = self.start("text")
        await asyncio.wait_for(text, 3)
        await self.wait(self.signal("text"))
        self.assertIsNotNone(self.stored("text"))
        self.assertEqual(self.bot.incoming_order.active_receipts, 0)
        release.set()
        await image

    async def test_own_image_still_finishes_before_event(self):
        """同一条图文消息仍先完成自身识图，不把未处理的文字提前分发。"""
        message, started, release = self.start("mixed", visual=True, text="合成关键词")
        await self.wait(started)
        self.assertFalse(self.signal("mixed").is_set())
        self.assertIsNone(self.stored("mixed"))
        release.set()
        await message
        await self.wait(self.signal("mixed"))
        self.assertIn("合成关键词", self.stored("mixed").processed_plain_text)

    async def test_filtered_text_does_not_reach_events(self):
        """过滤继续先于事件，丢弃后释放占位而不等待同会话的图片。"""
        self.bot_module.global_config.message_receive.ban_words = ["synthetic-ban"]
        image, started, release = self.start("image", visual=True)
        await self.wait(started)
        filtered, _, _ = self.start("filtered", text="synthetic-ban")
        await asyncio.wait_for(filtered, 3)
        self.assertFalse(self.signal("filtered").is_set())
        self.assertIsNone(self.stored("filtered"))
        self.assertEqual(self.bot.incoming_order.active_receipts, 1)
        release.set()
        await image


if __name__ == "__main__":
    case = MessageEventTests if IS_CHILD else IsolatedEventTests
    suite = (
        unittest.TestSuite([case(CASE)])
        if IS_CHILD and CASE
        else unittest.defaultTestLoader.loadTestsFromTestCase(case)
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    success = result.wasSuccessful() and not result.skipped
    if IS_CHILD and success:
        Path("events-tests.completed").touch()
    raise SystemExit(0 if success else 1)
