"""在新源码副本和数据库中验证未读批次、上下文边界及提示词编号。"""

import os
import asyncio
import gc
import shutil
import subprocess
import sys
import tempfile
import unittest
import weakref
from pathlib import Path


IS_CHILD = "--isolated-source" in sys.argv
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(IS_CHILD or sys.platform != "linux", "完整依赖测试在 Linux 隔离子进程执行")
class IsolatedBatchTests(unittest.TestCase):
    """复制运行源码后启动测试，不导入工作区配置或数据库。"""

    def test_isolated_contracts(self):
        """使用当前解释器和实际模型，在临时目录执行全部批次契约。"""
        with tempfile.TemporaryDirectory(prefix="maibot-batch-") as directory:
            source = Path(directory) / "source"
            shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copytree(ROOT / "template", source / "template")
            (source / "config").mkdir()
            for name in ("bot_config", "model_config"):
                shutil.copyfile(source / "template" / f"{name}_template.toml", source / "config" / f"{name}.toml")
            subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--isolated-source", str(source)],
                cwd=directory,
                check=True,
                timeout=120,
            )
            expected = unittest.defaultTestLoader.loadTestsFromTestCase(ChatBatchContractTests).countTestCases()
            self.assertEqual((source / "batch-tests.completed").read_text(encoding="utf-8"), str(expected))


@unittest.skipUnless(IS_CHILD, "只能在隔离源码子进程导入实际业务模型")
class ChatBatchContractTests(unittest.TestCase):
    """验证排序、容量、命令背景及清空后 ID 重用。"""

    @classmethod
    def setUpClass(cls):
        """加载隔离副本中的原始数据库模型和查询实现。"""
        source = Path(sys.argv[sys.argv.index("--isolated-source") + 1]).resolve()
        if not source.parent.name.startswith("maibot-batch-"):
            raise RuntimeError("测试源码必须位于专用临时目录")
        os.chdir(source)
        sys.path.insert(0, str(source))
        from src.common.database.database_model import ActionRecords, Messages
        from src.common.message_repository import read_chat_batch
        from src.config.config import global_config

        global_config.bot.qq_account = "9000"
        cls.Messages = Messages
        cls.Actions = ActionRecords
        cls.read = staticmethod(read_chat_batch)

    def setUp(self):
        """清理本测试专有数据库，为每个用例产生可重用的行 ID。"""
        self.Messages.delete().execute()
        self.Actions.delete().execute()

    def message(self, marker: str, stamp: float = 100.0, **extra):
        """使用真实必填字段插入虚构消息，返回实际数据库行。"""
        values = dict(
            message_id=marker,
            time=stamp,
            chat_id="group",
            chat_info_stream_id="group",
            chat_info_platform="qq",
            chat_info_user_platform="qq",
            chat_info_user_id="9100",
            chat_info_user_nickname="虚构用户",
            chat_info_group_id="9200",
            chat_info_group_name="虚构群",
            chat_info_create_time=1.0,
            chat_info_last_active_time=1.0,
            user_platform="qq",
            user_id="9100",
            user_nickname="虚构用户",
            processed_plain_text=marker,
            is_mentioned=True,
        )
        values.update(extra)
        return self.Messages.create(**values)

    def test_late_and_same_timestamps_are_not_skipped(self):
        """平台时间可以倒退或相同，实际新行仍按入库顺序接纳。"""
        first = self.message("first", 200.125)
        late = self.message("late", 100.125)
        same = self.message("same", 100.125)
        batch = self.read("group", first.id, 10, 18, "9000")
        self.assertEqual([row.id for row in batch.unread], [late.id, same.id])

    def test_burst_consumes_every_row_once(self):
        """四十一条输入分成五批，较早记录不会因取最新记录而消失。"""
        expected = [self.message(f"input-{number}", 100.0 - number).id for number in range(41)]
        consumed = []
        cursor = 0
        while True:
            batch = self.read("group", cursor, 10, 18, "9000")
            if not batch.unread:
                break
            self.assertLessEqual(len(batch.messages), 18)
            self.assertTrue({row.id for row in batch.unread} <= {row.id for row in batch.messages})
            consumed.extend(row.id for row in batch.unread)
            cursor = batch.unread[-1].id
        self.assertEqual(consumed, expected)

    def test_context_hides_future_input_but_includes_previous_reply(self):
        """历史回复可比积压输入更晚入库，下一批输入不能混入当前提示词。"""
        first = self.message("first")
        selected = self.message("selected")
        future = self.message("future")
        reply = self.message("reply", user_id="9000")
        batch = self.read("group", first.id, 1, 4, "9000")
        self.assertEqual([row.id for row in batch.unread], [selected.id])
        self.assertEqual(batch.available_count, 2)
        visible = {row.id for row in batch.messages}
        self.assertIn(reply.id, visible)
        self.assertNotIn(future.id, visible)
        self.message("later")
        self.assertEqual([row.message_id for row in batch.messages], ["first", "reply", "selected"])

    def test_command_visibility_and_self_filter(self):
        """级别一仅作为背景，级别二不可见，机器人和通知不触发。"""
        self.message("level-one", is_command=True, intercept_message_level=1)
        self.message("level-two", is_command=True, intercept_message_level=2)
        self.message("self", user_id="9000")
        self.message("notice", is_notify=True)
        normal = self.message("normal")
        batch = self.read("group", 0, 10, 18, "9000")
        self.assertEqual([row.id for row in batch.unread], [normal.id])
        self.assertEqual({row.message_id for row in batch.messages}, {"level-one", "self", "normal"})

    def test_clear_invalidates_old_snapshot(self):
        """清空后新行可以重用 ID，旧轮次不能覆盖新读取位置。"""
        from src.chat.round_context import ChatReadState

        first = self.message("first")
        state = ChatReadState("group", first.id, "qq")
        old = state.capture(18, None)
        self.assertTrue(state.admit(old))
        self.Messages.delete().execute()
        state.reset()
        new = self.message("new")
        self.assertEqual(new.id, first.id)
        self.assertTrue(state.has_unread())
        self.assertFalse(state.admit(old))
        self.assertEqual(state.after_id, 0)
        self.assertEqual(old.stored.unread[0].message_id, "first")
        self.assertEqual(state.capture(18, None).stored.unread[0].message_id, "new")

    def test_same_timestamp_prompt_identifiers(self):
        """同时刻消息拥有独立编号，Planner 的编号表与可见文本对应。"""
        from src.chat.round_context import ChatReadState

        first = self.message("unique-first")
        self.message("unique-second")
        context = ChatReadState("group", first.id, "qq").capture(18, None)
        text, mapping = context.planner_content()
        self.assertEqual(len(mapping), 2)
        self.assertNotEqual(mapping[0][0], mapping[1][0])
        for identifier, row in mapping:
            self.assertEqual(text.count(f"[{identifier}]"), 1)
            line = next(line for line in text.splitlines() if f"[{identifier}]" in line)
            self.assertIn(row.processed_plain_text, line)

    def test_restart_begins_with_new_input(self):
        """新聊天保留旧历史作背景，但不把旧输入全部当作未读。"""
        from src.chat.round_context import ChatReadState

        self.message("old")
        new = self.message("new", 1.0)
        state = ChatReadState("group", new.id, "qq")
        self.assertEqual([row.message_id for row in state.capture(18, None).stored.unread], ["new"])

    def test_query_failure_is_not_an_empty_batch(self):
        """真实 SQLite 查询失败必须传播，不能推进游标或报告零未读。"""
        from peewee import OperationalError
        from unittest.mock import patch

        with patch.object(self.Messages._meta.database, "execute_sql", side_effect=OperationalError("合成读库失败")):
            with self.assertRaises(OperationalError):
                self.read("group", 0, 10, 18, "9000")

    def test_transient_messages_keep_distinct_identifiers(self):
        """插件传入没有数据库 ID 的同时刻消息仍有独立编号。"""
        from src.chat.utils.chat_message_builder import build_readable_messages_with_id
        from src.common.data_models.database_data_model import DatabaseMessages

        messages = [
            DatabaseMessages(
                time=100.0,
                user_id="9100",
                user_platform="qq",
                user_nickname="虚构用户",
                processed_plain_text=f"transient-{number}",
            )
            for number in range(2)
        ]
        text, mapping = build_readable_messages_with_id(messages)
        self.assertEqual(len({identifier for identifier, _ in mapping}), 2)
        for identifier, message in mapping:
            line = next(line for line in text.splitlines() if f"[{identifier}]" in line)
            self.assertIn(message.processed_plain_text, line)

    def test_action_at_same_time_does_not_steal_identifier(self):
        """动作记录与输入同时间戳时，编号仍只属于真实消息。"""
        from src.chat.round_context import ChatReadState

        first = self.message("unique-input", 100.0)
        self.Actions.create(
            action_id="synthetic-action",
            time=100.0,
            action_name="wait",
            action_data="{}",
            action_done=True,
            action_build_into_prompt=True,
            action_prompt_display="synthetic-action-content",
            chat_id="group",
            chat_info_stream_id="group",
            chat_info_platform="qq",
        )
        text, mapping = ChatReadState("group", first.id, "qq").capture(18, None).planner_content()
        self.assertEqual(text.count(f"[{mapping[0][0]}]"), 1)
        action_line = next(line for line in text.splitlines() if "synthetic-action-content" in line)
        self.assertNotIn(f"[{mapping[0][0]}]", action_line)

    def test_new_query_reuses_existing_index(self):
        """检查实际批次查询的执行计划，确保不靠全表读取或临时排序实现游标。"""
        from unittest.mock import patch

        for number in range(30):
            self.message(f"query-{number}", chat_id=f"chat-{number % 3}")
        database = self.Messages._meta.database
        execute = database.execute_sql
        statements = []

        def observe(sql, params=None, **kwargs):
            """透传真实数据库语句，只记录本用例中的 SQL 和参数。"""
            statements.append((sql, params))
            return execute(sql, params, **kwargs)

        with patch.object(database, "execute_sql", side_effect=observe):
            self.read("chat-1", 3, 10, 18, "9000")
        sql, params = next(
            (sql, params)
            for sql, params in statements
            if sql.startswith("SELECT") and '"messages"' in sql and '"id" > ?' in sql
        )
        plan = " ".join(str(row[-1]) for row in execute("EXPLAIN QUERY PLAN " + sql, params).fetchall())
        self.assertIn("SEARCH", plan)
        self.assertIn("chat_id", plan)
        self.assertNotIn("TEMP B-TREE", plan)

    def test_read_state_does_not_hold_snapshot_messages(self):
        """读取状态只保存标量，已结束快照中的消息可以释放。"""
        from src.chat.round_context import ChatReadState

        row = self.message("release-me")
        state = ChatReadState("group", row.id, "qq")
        context = state.capture(18, None)
        sample = weakref.ref(context.stored.messages[0])
        state.admit(context)
        del context
        gc.collect()
        self.assertIsNone(sample())

    def test_empty_visual_result_is_retried_only_by_new_input(self):
        """首次空结果不追加请求；第二次明确输入同图时重新识别并保持原图片 ID。"""
        import base64
        import io
        from unittest.mock import AsyncMock, patch
        from PIL import Image
        from src.chat.utils.utils_image import get_image_manager
        from src.common.database.database_model import Images

        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (43, 67, 89)).save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

        async def check():
            """只替换外部模型结果，使用真实图片存储和共享任务代码。"""
            manager = get_image_manager()
            model = AsyncMock(side_effect=[(None, None), ("第二次识别成功", None)])
            with patch.object(manager.vlm, "generate_response_for_image", model):
                first_id, _ = await manager.process_image(encoded)
                self.assertEqual(model.await_count, 1)
                self.assertTrue(Images.get(Images.image_id == first_id).vlm_processed)
                self.assertFalse(Images.get(Images.image_id == first_id).description)
                second_id, _ = await manager.process_image(encoded)
                self.assertEqual(model.await_count, 2)
                self.assertEqual(first_id, second_id)
                row = Images.get(Images.image_id == first_id)
                self.assertEqual(row.description, "第二次识别成功")
                self.assertEqual(row.count, 2)
                self.assertFalse(manager._visual_tasks)

        asyncio.run(check())

    def test_config_upgrade_and_webui_schema(self):
        """真实配置合并补齐默认值，保留明确关闭，并向 WebUI 暴露布尔开关。"""
        import tomlkit
        from unittest.mock import patch
        from src.config import config as config_module
        from src.config.official_configs import ChatConfig
        from src.webui.config_schema import ConfigSchemaGenerator

        current_template = Path(config_module.TEMPLATE_DIR) / "bot_config_template.toml"
        for explicit in (None, False):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
                root = Path(directory)
                config_dir, template_dir = root / "config", root / "template"
                config_dir.mkdir()
                (template_dir / "compare").mkdir(parents=True)
                shutil.copyfile(current_template, template_dir / "bot_config_template.toml")
                old = tomlkit.parse(current_template.read_text(encoding="utf-8"))
                old["inner"]["version"] = "7.3.5"
                old["chat"].pop("wait_for_image_recognition")
                (template_dir / "compare/bot_config_template.toml").write_text(tomlkit.dumps(old), encoding="utf-8")
                if explicit is not None:
                    old["chat"]["wait_for_image_recognition"] = explicit
                old["chat"]["talk_value"] = 0.42
                target = config_dir / "bot_config.toml"
                target.write_text(tomlkit.dumps(old), encoding="utf-8")
                with (
                    patch.object(config_module, "CONFIG_DIR", str(config_dir)),
                    patch.object(config_module, "TEMPLATE_DIR", str(template_dir)),
                ):
                    config_module.update_config()
                updated = tomlkit.parse(target.read_text(encoding="utf-8"))
                self.assertEqual(updated["inner"]["version"], "7.3.6")
                self.assertEqual(updated["chat"]["wait_for_image_recognition"], True if explicit is None else explicit)
                self.assertEqual(updated["chat"]["talk_value"], 0.42)
                self.assertEqual(len(list((config_dir / "old").glob("*.toml"))), 1)
        field = next(
            field
            for field in ConfigSchemaGenerator.generate_schema(ChatConfig)["fields"]
            if field["name"] == "wait_for_image_recognition"
        )
        self.assertEqual(field["type"], "boolean")
        self.assertTrue(field["default"])
        self.assertIn("等待识图完成", field["description"])

    def test_visual_classification_excludes_voice_and_text_emoji(self):
        """最终片段分类涵盖嵌套图片，但不把语音或普通文字表情当作视觉阻塞。"""
        from maim_message import Seg
        from src.chat.message_receive.message import contains_visual_segments

        self.assertFalse(contains_visual_segments(Seg("voice", "synthetic")))
        self.assertFalse(contains_visual_segments(Seg("text", "🙂")))
        self.assertTrue(contains_visual_segments(Seg("emoji", "synthetic")))
        self.assertTrue(contains_visual_segments(Seg("seglist", [Seg("text", "a"), Seg("image", "b")])))

    def test_shared_visual_work_and_waiter_cleanup(self):
        """实际共享调度方法只占一次并发名额，取消重复等待者后仍能完成并清空状态。"""
        from src.chat.utils.utils_image import get_image_manager

        async def check():
            """通过事件控制模拟外部识别工作，不替换共享任务方法。"""
            manager = get_image_manager()
            starts = [asyncio.Event() for _ in range(4)]
            finishes = [asyncio.Event() for _ in range(4)]
            calls = [0] * 4

            async def work(number):
                """记录真正获得并发名额的工作，并等待测试释放。"""
                calls[number] += 1
                starts[number].set()
                await finishes[number].wait()
                return number

            tasks = [asyncio.create_task(manager._share_visual("test", "0", lambda: work(0)))]
            duplicate = asyncio.create_task(manager._share_visual("test", "0", lambda: work(0)))
            tasks.extend(
                asyncio.create_task(manager._share_visual("test", str(number), lambda number=number: work(number)))
                for number in range(1, 4)
            )
            await asyncio.wait_for(asyncio.gather(*(item.wait() for item in starts[:3])), 2)
            self.assertFalse(starts[3].is_set())
            duplicate.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await duplicate
            finishes[0].set()
            await asyncio.wait_for(starts[3].wait(), 2)
            for event in finishes:
                event.set()
            self.assertEqual(await asyncio.gather(*tasks), [0, 1, 2, 3])
            self.assertEqual(calls, [1, 1, 1, 1])
            self.assertFalse(manager._visual_tasks)

        asyncio.run(check())

    def test_storage_failure_does_not_start_chat(self):
        """真实 SQLite 只读失败时，不通知聊天循环，不产生已发布的消息行。"""
        from maim_message import GroupInfo, UserInfo
        from src.chat.heart_flow.heartflow import heartflow
        from src.chat.heart_flow.heartflow_message_processor import HeartFCMessageReceiver
        from src.chat.message_receive.chat_stream import ChatStream
        from src.chat.message_receive.message import MessageRecv

        user = UserInfo(platform="qq", user_id="9100", user_nickname="虚构用户")
        group = GroupInfo(platform="qq", group_id="9200", group_name="虚构群")
        message = MessageRecv(
            {
                "message_info": {
                    "platform": "qq",
                    "message_id": "readonly-failure",
                    "time": 100.0,
                    "user_info": user.to_dict(),
                    "group_info": group.to_dict(),
                },
                "message_segment": {"type": "text", "data": "合成存储失败"},
                "processed_plain_text": "合成存储失败",
            }
        )
        message.update_chat_stream(ChatStream("readonly-stream", "qq", user, group))
        before = set(heartflow.heartflow_chat_list)
        database = self.Messages._meta.database
        database.execute_sql("PRAGMA query_only = ON")
        try:
            asyncio.run(HeartFCMessageReceiver().process_message(message))
        finally:
            database.execute_sql("PRAGMA query_only = OFF")
        self.assertFalse(self.Messages.select().where(self.Messages.message_id == "readonly-failure").exists())
        self.assertEqual(set(heartflow.heartflow_chat_list), before)

    def test_replyer_exception_paths_resolve_lazy_traceback(self):
        """非法回复目标在构造提示词前失败，覆盖两个真实异常路径且不调用模型。"""
        from maim_message import GroupInfo, UserInfo
        from src.chat.message_receive.chat_stream import ChatStream

        # 从公开生成 API 进入，遵循宿主初始化顺序，避免直接导入内部模块造成循环导入。
        from src.plugin_system.apis import generator_api

        async def check():
            """只向真实生成方法提供非法目标；异常发生在任何模型请求之前。"""
            for cls in (generator_api.DefaultReplyer, generator_api.PrivateReplyer):
                replyer = cls.__new__(cls)
                user = UserInfo(platform="qq", user_id="9100", user_nickname="虚构用户")
                group = GroupInfo(platform="qq", group_id="9200", group_name="虚构群")
                replyer.chat_stream = ChatStream("invalid-reply", "qq", user, group)
                success, _ = await replyer.generate_reply_with_context(
                    reply_message=object(), from_plugin=True, log_reply=False
                )
                self.assertFalse(success)

        asyncio.run(check())


if __name__ == "__main__":
    if IS_CHILD:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ChatBatchContractTests)
        expected_count = suite.countTestCases()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if result.testsRun != expected_count or result.skipped:
            raise SystemExit("隔离测试没有完整执行")
        if result.wasSuccessful():
            Path("batch-tests.completed").write_text(str(result.testsRun), encoding="utf-8")
        raise SystemExit(not result.wasSuccessful())
    unittest.main()
