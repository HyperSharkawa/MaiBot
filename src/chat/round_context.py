"""将一轮聊天使用的输入和读取进度分开，避免异步工作期间上下文漂移。"""

import copy
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.common.data_models.database_data_model import DatabaseMessages
from src.common.message_repository import StoredChatBatch, count_unread_messages, has_unread_messages, read_chat_batch
from src.config.config import global_config

if TYPE_CHECKING:
    from src.chat.message_receive.message import MessageRecv


@dataclass(frozen=True, slots=True)
class RoundContext:
    """只在当前轮次传递的有界快照；不能写入动作参数或轮次历史。"""

    generation: int
    after_id: int
    started_at: float
    planner_limit: int
    stored: StoredChatBatch
    assigned_ids: tuple[tuple[str, DatabaseMessages], ...]
    event_message: "MessageRecv | None"

    @property
    def unread_ids(self) -> frozenset[int]:
        """返回本轮接纳的输入身份，独立于平台消息时间和回执 ID。"""
        return frozenset(message.id for message in self.stored.unread)

    @property
    def template_name(self) -> str | None:
        """读取本轮捕获的模板选择，不让后续消息改写当前轮上下文。"""
        if self.event_message is None:
            return None
        info = self.event_message.message_info.template_info
        return info.template_name if info and not info.template_default else None

    def messages(self, limit: int) -> list[DatabaseMessages]:
        """按已有窗口大小选取快照；主窗口始终为本批未读保留足够容量。"""
        return list(self.stored.messages[-max(1, limit) :])

    def format_messages(self, messages: list[DatabaseMessages], **options) -> str:
        """格式化本轮选出的消息，禁止格式化器重新读取动作或按时间重排未读区。"""
        from src.chat.utils.chat_message_builder import build_readable_messages

        return build_readable_messages(
            messages, unread_ids=self.unread_ids, action_records=self.stored.actions, **options
        )

    def planner_content(self, *, short: bool = False) -> tuple[str, list[tuple[str, DatabaseMessages]]]:
        """格式化固定的 Planner 上下文与编号，调用期间不再查询消息或动作。"""
        from src.chat.utils.chat_message_builder import build_readable_messages_with_id

        messages = self.messages(max(1, self.planner_limit // 2) if short else self.planner_limit)
        selected = {id(message) for message in messages}
        return build_readable_messages_with_id(
            messages,
            timestamp_mode="normal_no_YMD",
            truncate=not short,
            show_actions=not short,
            unread_ids=self.unread_ids,
            action_records=self.stored.actions,
            assigned_ids=[item for item in self.assigned_ids if id(item[1]) in selected],
        )


class ChatReadState:
    """保存一个现有聊天实例的读取位置，不积累已完成轮次或消息对象。"""

    def __init__(self, chat_id: str, first_message_id: int, platform: str) -> None:
        """从本进程首条发布的输入开始，避免漏首条或重新回复旧数据库历史。"""
        from src.chat.utils.utils import get_current_platform_account, parse_platform_accounts

        self.chat_id = chat_id
        self.after_id = first_message_id - 1
        self.generation = 0
        accounts = parse_platform_accounts(global_config.bot.platforms)
        primary = str(global_config.bot.qq_account or "")
        account = get_current_platform_account(platform, accounts, primary)
        # 与 is_bot_self 一致：Telegram 未配置账号时不猜测，其他平台沿用主账号。
        self.bot_user_id = account or (None if platform == "telegram" else primary)

    def capture(self, context_limit: int, event_message: "MessageRecv | None") -> RoundContext:
        """在异步模型工作前取得快照；查询错误向上传递，消费位置保持不变。"""
        from src.chat.utils.utils import assign_message_ids

        planner_limit = max(1, int(context_limit * 0.6))
        stored = read_chat_batch(self.chat_id, self.after_id, min(20, planner_limit), context_limit, self.bot_user_id)
        return RoundContext(
            generation=self.generation,
            after_id=self.after_id,
            started_at=time.time(),
            planner_limit=planner_limit,
            stored=stored,
            assigned_ids=tuple(assign_message_ids(list(stored.messages))),
            event_message=copy.deepcopy(event_message),
        )

    def admit(self, context: RoundContext) -> bool:
        """只推进实际接纳的批次；清空历史使旧代次失效，不能覆盖新读取位置。"""
        if context.generation != self.generation:
            return False
        if context.stored.unread:
            self.after_id = context.stored.unread[-1].id
        return True

    def reset(self) -> None:
        """清空本会话历史成功后重新开始读取，允许 SQLite 重用行 ID。"""
        self.generation += 1
        self.after_id = 0

    def has_unread(self) -> bool:
        """查询实际条件，不因事件被设置就认为存在可决策输入。"""
        return has_unread_messages(self.chat_id, self.after_id, self.bot_user_id)

    def count_unread(self) -> int:
        """计算本批之后的输入数量，供发送前引用启发式使用。"""
        return count_unread_messages(self.chat_id, self.after_id, self.bot_user_id)
