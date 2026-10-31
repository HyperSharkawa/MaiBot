"""验证图片发布边界；不导入配置、数据库或真实聊天服务。"""

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

# 包入口会初始化聊天服务；纯协调器测试只加载实际目标文件，不伪造依赖模块。
_path = Path(__file__).resolve().parents[1] / "src/chat/message_receive/incoming_order.py"
_spec = importlib.util.spec_from_file_location("incoming_order_under_test", _path)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)
IncomingOrder = _module.IncomingOrder


class IncomingOrderTests(unittest.IsolatedAsyncioTestCase):
    """以显式事件切换验证顺序、会话隔离和清理，不依赖时间猜测。"""

    async def test_later_image_cannot_overtake_earlier_image(self):
        """后图先识别完成时，图后的文字仍须等待前图发布。"""
        order = IncomingOrder()
        image1, text3, image2, text4 = [order.reserve("group") for _ in range(4)]
        image1.classify(True)
        text3.classify(False)
        image2.classify(True)
        text4.classify(False)
        waiting = [asyncio.create_task(item.wait_until_publishable()) for item in (text3, image2, text4)]
        await asyncio.sleep(0)
        self.assertTrue(all(not task.done() for task in waiting))
        await image1.wait_until_publishable()
        image1.close()
        await asyncio.gather(*waiting[:2])
        self.assertFalse(waiting[2].done())
        text3.close()
        image2.close()
        await waiting[2]
        text4.close()
        self.assertEqual(order.active_sessions, 0)

    async def test_preprocessing_and_nonvisual_work(self):
        """未分类消息先占位；确认没有图片后，慢语音等工作不阻塞后续文字。"""
        order = IncomingOrder()
        first, second = order.reserve("private"), order.reserve("private")
        second.classify(False)
        waiting = asyncio.create_task(second.wait_until_publishable())
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        first.classify(False)
        await waiting
        second.close()
        later = order.reserve("private")
        self.assertGreater(later.sequence, second.sequence)
        self.assertEqual(order.active_receipts, 2)
        first.close()
        later.close()
        self.assertEqual(order.active_sessions, 0)

    async def test_other_session_and_cancelled_waiter(self):
        """另一会话可以发布；取消一条等待消息后仍能清理并继续当前会话。"""
        order = IncomingOrder()
        image = order.reserve("group-a")
        image.classify(True)
        text = order.reserve("group-a")
        text.classify(False)
        other = order.reserve("group-b")
        other.classify(False)
        await other.wait_until_publishable()
        other.close()
        waiting = asyncio.create_task(text.wait_until_publishable())
        await asyncio.sleep(0)
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        text.close()
        image.close()
        self.assertEqual(order.active_receipts, 0)
        self.assertEqual(order.active_sessions, 0)

    async def test_failure_releases_the_next_message(self):
        """失败图片结束接收后释放后续消息，重复清理不会破坏计数。"""
        order = IncomingOrder()
        image, text = order.reserve("group"), order.reserve("group")
        image.classify(True)
        text.classify(False)
        waiting = asyncio.create_task(text.wait_until_publishable())
        await asyncio.sleep(0)
        image.close()
        image.close()
        await waiting
        text.close()
        self.assertEqual(order.active_sessions, 0)


if __name__ == "__main__":
    unittest.main()
