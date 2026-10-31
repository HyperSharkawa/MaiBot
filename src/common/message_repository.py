import traceback
from dataclasses import dataclass

from typing import List, Any, Optional
from peewee import Model  # 添加 Peewee Model 导入

from src.config.config import global_config
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.database.database_model import ActionRecords, Messages
from src.common.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StoredChatBatch:
    """同一读取事务内得到的有界消息批次；不包含延迟加载的数据库查询。"""

    unread: tuple[DatabaseMessages, ...]
    messages: tuple[DatabaseMessages, ...]
    actions: tuple[dict[str, Any], ...]
    available_count: int
    high_water_id: int


def _chat_input_condition(bot_user_id: str | None):
    """构造普通聊天触发条件；命令级别一只可作背景，不能触发回复。"""
    condition = (Messages.message_id != "notice") & (~Messages.is_notify) & (Messages.intercept_message_level <= 0)
    if bot_user_id is not None:
        condition &= Messages.user_id != bot_user_id
    return condition


def has_unread_messages(chat_id: str, after_id: int, bot_user_id: str | None) -> bool:
    """检查实际未读条件；数据库失败向上传递，不伪装成没有新消息。"""
    return (
        Messages.select(Messages.id)
        .where((Messages.chat_id == chat_id) & (Messages.id > after_id) & _chat_input_condition(bot_user_id))
        .exists()
    )


def count_unread_messages(chat_id: str, after_id: int, bot_user_id: str | None) -> int:
    """按入库边界计算新输入数量，用于发送前的引用判断。"""
    return (
        Messages.select()
        .where((Messages.chat_id == chat_id) & (Messages.id > after_id) & _chat_input_condition(bot_user_id))
        .count()
    )


def read_chat_batch(
    chat_id: str, after_id: int, batch_limit: int, context_limit: int, bot_user_id: str | None
) -> StoredChatBatch:
    """读取最早未读及有界背景；失败不返回空批次，不修改消费游标。"""
    if not 1 <= batch_limit <= context_limit:
        raise ValueError("批次容量必须为正数且不大于上下文容量")
    trigger = _chat_input_condition(bot_user_id)
    with Messages._meta.database.atomic():
        # 普通数量门槛最高为二，容量为一时仍需要知道后面是否还有输入。
        available = list(
            Messages.select()
            .where((Messages.chat_id == chat_id) & (Messages.id > after_id) & trigger)
            .order_by(Messages.id.asc())
            .limit(max(2, batch_limit))
        )
        unread_models = available[:batch_limit]
        unread_ids = [row.id for row in unread_models]
        high_water_id = (
            Messages.select(Messages.id).where(Messages.chat_id == chat_id).order_by(Messages.id.desc()).scalar() or 0
        )
        history_limit = context_limit - len(unread_models)
        history = []
        if history_limit:
            # 尚未接纳的普通输入不能混进本批；机器人回复和级别一命令仍可作为背景。
            history = list(
                Messages.select()
                .where(
                    (Messages.chat_id == chat_id)
                    & (Messages.id <= high_water_id)
                    & ((Messages.id <= after_id) | (~trigger))
                    & (Messages.message_id != "notice")
                    & (~Messages.is_notify)
                    & (Messages.intercept_message_level <= 1)
                    & (Messages.id.not_in(unread_ids))
                )
                .order_by(Messages.id.desc())
                .limit(history_limit)
            )
            history.reverse()
        messages = tuple(_model_to_instance(row) for row in [*history, *unread_models])
        actions = []
        if messages:
            actions = list(
                ActionRecords.select()
                .where(
                    (ActionRecords.chat_id == chat_id)
                    & ActionRecords.action_build_into_prompt
                    & (ActionRecords.time >= min(message.time for message in messages))
                )
                .order_by(ActionRecords.id.desc())
                .limit(context_limit)
                .dicts()
            )
            actions.reverse()
        return StoredChatBatch(
            unread=tuple(_model_to_instance(row) for row in unread_models),
            messages=messages,
            actions=tuple(actions),
            available_count=len(available),
            high_water_id=high_water_id,
        )


def _model_to_instance(model_instance: Model) -> DatabaseMessages:
    """
    将 Peewee 模型实例转换为字典。
    """
    return DatabaseMessages(**model_instance.__data__)


def find_messages(
    message_filter: dict[str, Any],
    sort: Optional[List[tuple[str, int]]] = None,
    limit: int = 0,
    limit_mode: str = "latest",
    filter_bot=False,
    filter_command=False,
    filter_intercept_message_level: Optional[int] = None,
) -> List[DatabaseMessages]:
    """
    根据提供的过滤器、排序和限制条件查找消息。

    Args:
        message_filter: 查询过滤器字典，键为模型字段名，值为期望值或包含操作符的字典 (例如 {'$gt': value}).
        sort: 排序条件列表，例如 [('time', 1)] (1 for asc, -1 for desc)。仅在 limit 为 0 时生效。
        limit: 返回的最大文档数，0表示不限制。
        limit_mode: 当 limit > 0 时生效。 'earliest' 表示获取最早的记录， 'latest' 表示获取最新的记录（结果仍按时间正序排列）。默认为 'latest'。

    Returns:
        消息字典列表，如果出错则返回空列表。
    """
    try:
        query = Messages.select()

        # 应用过滤器
        if message_filter:
            conditions = []
            for key, value in message_filter.items():
                if hasattr(Messages, key):
                    field = getattr(Messages, key)
                    if isinstance(value, dict):
                        # 处理 MongoDB 风格的操作符
                        for op, op_value in value.items():
                            if op == "$gt":
                                conditions.append(field > op_value)
                            elif op == "$lt":
                                conditions.append(field < op_value)
                            elif op == "$gte":
                                conditions.append(field >= op_value)
                            elif op == "$lte":
                                conditions.append(field <= op_value)
                            elif op == "$ne":
                                conditions.append(field != op_value)
                            elif op == "$in":
                                conditions.append(field.in_(op_value))
                            elif op == "$nin":
                                conditions.append(field.not_in(op_value))
                            else:
                                logger.warning(f"过滤器中遇到未知操作符 '{op}' (字段: '{key}')。将跳过此操作符。")
                    else:
                        # 直接相等比较
                        conditions.append(field == value)
                else:
                    logger.warning(f"过滤器键 '{key}' 在 Messages 模型中未找到。将跳过此条件。")
            if conditions:
                query = query.where(*conditions)

        # 排除 id 为 "notice" 的消息
        query = query.where(Messages.message_id != "notice")

        if filter_bot:
            query = query.where(Messages.user_id != global_config.bot.qq_account)

        if filter_command:
            # 使用按位取反构造 Peewee 的 NOT 条件，避免直接与 False 比较
            query = query.where(~Messages.is_command)

        if filter_intercept_message_level is not None:
            # 过滤掉所有 intercept_message_level > filter_intercept_message_level 的消息
            query = query.where(Messages.intercept_message_level <= filter_intercept_message_level)

        if limit > 0:
            if limit_mode == "earliest":
                # 获取时间最早的 limit 条记录，已经是正序
                query = query.order_by(Messages.time.asc()).limit(limit)
                peewee_results = list(query)
            else:  # 默认为 'latest'
                # 获取时间最晚的 limit 条记录
                query = query.order_by(Messages.time.desc()).limit(limit)
                latest_results_peewee = list(query)
                # 将结果按时间正序排列
                peewee_results = sorted(latest_results_peewee, key=lambda msg: msg.time)
        else:
            # limit 为 0 时，应用传入的 sort 参数
            if sort:
                peewee_sort_terms = []
                for field_name, direction in sort:
                    if hasattr(Messages, field_name):
                        field = getattr(Messages, field_name)
                        if direction == 1:  # ASC
                            peewee_sort_terms.append(field.asc())
                        elif direction == -1:  # DESC
                            peewee_sort_terms.append(field.desc())
                        else:
                            logger.warning(f"字段 '{field_name}' 的排序方向 '{direction}' 无效。将跳过此排序条件。")
                    else:
                        logger.warning(f"排序字段 '{field_name}' 在 Messages 模型中未找到。将跳过此排序条件。")
                if peewee_sort_terms:
                    query = query.order_by(*peewee_sort_terms)
            peewee_results = list(query)

        return [_model_to_instance(msg) for msg in peewee_results]
    except Exception as e:
        log_message = (
            f"使用 Peewee 查找消息失败 (filter={message_filter}, sort={sort}, limit={limit}, limit_mode={limit_mode}): {e}\n"
            + traceback.format_exc()
        )
        logger.error(log_message)
        return []


def count_messages(message_filter: dict[str, Any]) -> int:
    """
    根据提供的过滤器计算消息数量。

    Args:
        message_filter: 查询过滤器字典，键为模型字段名，值为期望值或包含操作符的字典 (例如 {'$gt': value}).

    Returns:
        符合条件的消息数量，如果出错则返回 0。
    """
    try:
        query = Messages.select()

        # 应用过滤器
        if message_filter:
            conditions = []
            for key, value in message_filter.items():
                if hasattr(Messages, key):
                    field = getattr(Messages, key)
                    if isinstance(value, dict):
                        # 处理 MongoDB 风格的操作符
                        for op, op_value in value.items():
                            if op == "$gt":
                                conditions.append(field > op_value)
                            elif op == "$lt":
                                conditions.append(field < op_value)
                            elif op == "$gte":
                                conditions.append(field >= op_value)
                            elif op == "$lte":
                                conditions.append(field <= op_value)
                            elif op == "$ne":
                                conditions.append(field != op_value)
                            elif op == "$in":
                                conditions.append(field.in_(op_value))
                            elif op == "$nin":
                                conditions.append(field.not_in(op_value))
                            else:
                                logger.warning(
                                    f"计数时，过滤器中遇到未知操作符 '{op}' (字段: '{key}')。将跳过此操作符。"
                                )
                    else:
                        # 直接相等比较
                        conditions.append(field == value)
                else:
                    logger.warning(f"计数时，过滤器键 '{key}' 在 Messages 模型中未找到。将跳过此条件。")
            if conditions:
                query = query.where(*conditions)

        # 排除 id 为 "notice" 的消息
        query = query.where(Messages.message_id != "notice")

        count = query.count()
        return count
    except Exception as e:
        log_message = f"使用 Peewee 计数消息失败 (message_filter={message_filter}): {e}\n{traceback.format_exc()}"
        logger.error(log_message)
        return 0


# 你可以在这里添加更多与 messages 集合相关的数据库操作函数，例如 find_one_message, insert_message 等。
# 注意：对于 Peewee，插入操作通常是 Messages.create(...) 或 instance.save()。
# 查找单个消息可以是 Messages.get_or_none(...) 或 query.first()。
