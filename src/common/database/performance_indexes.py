"""补齐经过查询对照验证的索引；调用方负责选择数据库与连接生命周期。"""

import re
import time
from dataclasses import dataclass

from peewee import DatabaseError


@dataclass(frozen=True)
class IndexSpec:
    """固定性能索引定义，不接受聊天输入或外部 SQL。"""

    name: str
    table: str
    columns: tuple[str, ...]
    predicate: str = ""


@dataclass(frozen=True)
class IndexResult:
    """一次启动检查的结果；失败不会被记录为索引就绪。"""

    name: str
    status: str
    actual_name: str | None = None
    error: str | None = None


PERFORMANCE_INDEXES = (
    IndexSpec("idx_messages_chat_time", "messages", ("chat_id", "time")),
    IndexSpec("idx_action_records_chat_time", "action_records", ("chat_id", "time")),
    IndexSpec("idx_expression_visible_chat_id", "expression", ("chat_id",), "checked AND NOT rejected"),
    IndexSpec("idx_jargon_content", "jargon", ("content",)),
)


class IndexDefinitionConflict(ValueError):
    """同名索引与固定定义冲突，必须保留原索引并交由维护者处理。"""


def _predicate_matches(sql: str | None, spec: IndexSpec) -> bool:
    """仅认可当前固定谓词的等同写法，不推断复杂 SQL 的逻辑等价性。"""
    if not spec.predicate:
        return True
    match = re.search(r"\bWHERE\s+(.+?)\s*;?\Z", sql or "", flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        return False
    checked = r'(?:checked|"checked"|`checked`|\[checked\])'
    rejected = r'(?:rejected|"rejected"|`rejected`|\[rejected\])'
    return re.fullmatch(rf"{checked}\s+AND\s+NOT\s+{rejected}", match[1], flags=re.IGNORECASE) is not None


def _matches(database, name: str, spec: IndexSpec) -> bool:
    """核对表、唯一性、部分条件以及全部键列，避免仅凭名称误认已优化。"""
    record = database.execute_sql(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone()
    if record is None or record[0] != spec.table:
        return False
    attributes = database.execute_sql(
        'SELECT "unique", partial FROM pragma_index_list(?) WHERE name = ?', (spec.table, name)
    ).fetchone()
    if attributes != (0, int(bool(spec.predicate))):
        return False
    keys = database.execute_sql(
        'SELECT name, "desc", coll FROM pragma_index_xinfo(?) WHERE key = 1 ORDER BY seqno', (name,)
    ).fetchall()
    expected = [(column, 0, "BINARY") for column in spec.columns]
    return keys == expected and _predicate_matches(record[1], spec)


def _ensure_one(database, spec: IndexSpec) -> IndexResult:
    """在单项事务中复用或创建索引；任何失败均由事务回滚。"""
    with database.atomic():
        named = database.execute_sql(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", (spec.name,)
        ).fetchone()
        if named:
            if not _matches(database, spec.name, spec):
                raise IndexDefinitionConflict(f"索引 {spec.name} 的现有定义不匹配，已保留原索引")
            return IndexResult(spec.name, "existing", spec.name)
        names = database.execute_sql("SELECT name FROM pragma_index_list(?)", (spec.table,)).fetchall()
        # 旧脚本可能使用其他索引名，只复用能明确核实的相同定义。
        for (name,) in names:
            if _matches(database, name, spec):
                return IndexResult(spec.name, "existing", name)
        columns = ", ".join(f'"{column}"' for column in spec.columns)
        statement = f'CREATE INDEX "{spec.name}" ON "{spec.table}" ({columns})'
        if spec.predicate:
            statement += f" WHERE {spec.predicate}"
        database.execute_sql(statement)
    return IndexResult(spec.name, "created", spec.name)


def ensure_performance_indexes(database, logger) -> list[IndexResult]:
    """检查四个固定索引，记录数据库错误后继续；不重试、不修改业务数据。"""
    results = []
    for spec in PERFORMANCE_INDEXES:
        started = time.monotonic()
        try:
            result = _ensure_one(database, spec)
        except (DatabaseError, IndexDefinitionConflict) as error:
            result = IndexResult(spec.name, "failed", error=str(error))
            logger.error(f"性能索引检查失败：{spec.name}，表 {spec.table}，原因：{error}")
        if result.status == "created":
            logger.info(f"性能索引已创建：{spec.name}，表 {spec.table}，耗时 {time.monotonic() - started:.3f} 秒")
        elif result.status == "existing":
            logger.debug(f"性能索引已就绪：{spec.name}，复用 {result.actual_name}")
        results.append(result)
    counts = {
        status: sum(result.status == status for result in results) for status in ("created", "existing", "failed")
    }
    logger.info(f"性能索引检查结束：新建 {counts['created']}，复用 {counts['existing']}，失败 {counts['failed']}")
    return results
