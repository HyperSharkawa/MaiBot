"""只协调正在接收的消息，保证未完成图片之后的普通聊天不会提前发布。"""

import asyncio
from dataclasses import dataclass, field


@dataclass(slots=True)
class _Session:
    """保存一个会话尚未结束的接收任务；空闲后由协调器立即移除。"""

    next_sequence: int = 0
    active_count: int = 0
    blockers: set[int] = field(default_factory=set)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def notify(self) -> None:
        """唤醒读取旧状态的等待者，并为后续状态变化建立新的通知。"""
        previous = self.changed
        self.changed = asyncio.Event()
        previous.set()


@dataclass(slots=True)
class IncomingReceipt:
    """标识一条在途消息；只持有顺序状态，不保存消息或图片内容。"""

    stream_id: str
    sequence: int
    _session: _Session
    _coordinator: "IncomingOrder"
    _closed: bool = False

    def classify(self, has_visual: bool) -> None:
        """在预处理插件结束后分类；非图片立即解除对后续消息的阻塞。"""
        if self._closed:
            raise RuntimeError("已经结束的接收任务不能重新分类")
        if not has_visual:
            self._session.blockers.discard(self.sequence)
            self._session.notify()

    async def wait_until_publishable(self) -> None:
        """等待所有较早图片发布或终止；不阻止任何图片识别任务运行。"""
        if self._closed:
            raise RuntimeError("已经结束的接收任务不能再次发布")
        # 条件检查与取得通知之间没有异步切换，因此不会错过解除阻塞的通知。
        while any(sequence < self.sequence for sequence in self._session.blockers):
            changed = self._session.changed
            await changed.wait()

    def close(self) -> None:
        """结束成功、丢弃或取消的消息，并释放本会话不再需要的状态。"""
        if self._closed:
            return
        self._closed = True
        session = self._session
        session.blockers.discard(self.sequence)
        session.active_count -= 1
        session.notify()
        # 非图片虽然不构成阻塞，但仍可能在等待；它结束前不能重新编号。
        if session.active_count == 0:
            del self._coordinator._sessions[self.stream_id]


class IncomingOrder:
    """按会话隔离接收顺序；所有操作由接收消息的同一事件循环调用。"""

    def __init__(self) -> None:
        """建立只包含活跃接收会话的状态表。"""
        self._sessions: dict[str, _Session] = {}

    def reserve(self, stream_id: str) -> IncomingReceipt:
        """在接收路径第一次异步等待前占位，防止预处理插件插图后被越过。"""
        session = self._sessions.setdefault(stream_id, _Session())
        sequence = session.next_sequence
        session.next_sequence += 1
        session.active_count += 1
        session.blockers.add(sequence)
        return IncomingReceipt(stream_id, sequence, session, self)

    @property
    def active_sessions(self) -> int:
        """返回当前仍有接收工作的会话数，供状态检查使用。"""
        return len(self._sessions)

    @property
    def active_receipts(self) -> int:
        """返回尚未结束的接收数量，不保留已完成消息历史。"""
        return sum(session.active_count for session in self._sessions.values())
