"""使用真实模型导出的完整 SQLite 结构验证索引；全部数据库都位于临时目录。"""

import json
import logging
import os
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from peewee import OperationalError, SqliteDatabase

ROOT = Path(__file__).resolve().parents[1]


def export_schema(source: Path, output: Path) -> None:
    """在隔离源码内执行原初始化，导出完整表结构和真实 Peewee 查询。"""
    os.chdir(source)
    sys.path.insert(0, str(source))
    from src.common.database.database import db
    from src.common.database.database_model import ActionRecords, Expression, Jargon, Messages

    queries = [
        Messages.select()
        .where(
            (Messages.chat_id == "c17")
            & (Messages.time > 95000)
            & (Messages.time < 100000)
            & (Messages.message_id != "notice")
            & (Messages.user_id != "900000001")
            & (Messages.intercept_message_level <= 0)
        )
        .order_by(Messages.time.desc())
        .limit(20),
        ActionRecords.select()
        .where((ActionRecords.chat_id == "c17") & (ActionRecords.time > 95000) & (ActionRecords.time < 100000))
        .order_by(ActionRecords.time.desc())
        .limit(20),
        Expression.select().where(
            Expression.chat_id.in_(["c13", "c17", "c25"])
            & (~Expression.rejected)
            & (Expression.count > 1)
            & Expression.checked
        ),
        Jargon.select().where(Jargon.content == "term17001"),
    ]
    with db.connection_context():
        schema = db.execute_sql(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    output.write_text(json.dumps({"schema": schema, "queries": [query.sql() for query in queries]}), encoding="utf-8")


def isolated_schema(directory: Path) -> dict:
    """复制必要源码后单独导入，避免模型导入期初始化触及工作区数据库。"""
    source = directory / "source"
    shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "template", source / "template")
    output = directory / "schema.json"
    subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--export-schema", str(source), str(output)],
        check=True,
        cwd=directory,
        stdout=subprocess.DEVNULL,
    )
    return json.loads(output.read_text(encoding="utf-8"))


class PerformanceIndexTests(unittest.TestCase):
    """验证真实表上的幂等、事务、失败继续和查询数据不变性。"""

    @classmethod
    def setUpClass(cls):
        """从本次候选的模型和原启动流程取得结构，随后只使用临时库。"""
        cls.directory = tempfile.TemporaryDirectory(prefix="maibot-index-tests-")
        cls.exported = isolated_schema(Path(cls.directory.name))
        sys.path.insert(0, str(ROOT))
        from src.common.database import performance_indexes

        cls.module = performance_indexes
        cls.logger = logging.getLogger("index-test")
        cls.logger.addHandler(logging.NullHandler())

    @classmethod
    def tearDownClass(cls):
        """释放本次测试创建的目录，不操作外部数据库。"""
        cls.directory.cleanup()

    def setUp(self):
        """每个用例使用独立内存库和原模型的基础索引。"""
        self.db = SqliteDatabase(":memory:")
        self.db.connect()
        self.create_schema(self.db)

    def tearDown(self):
        """关闭当前用例连接。"""
        self.db.close()

    @classmethod
    def create_schema(cls, db):
        """重放真实模型的表和基础索引，排除本次待验证的性能索引。"""
        names = {spec.name for spec in cls.module.PERFORMANCE_INDEXES}
        for kind, name, sql in cls.exported["schema"]:
            if kind == "table" or (kind == "index" and name not in names):
                db.execute_sql(sql)

    def ensure(self):
        """调用生产索引函数，不替换检查或创建实现。"""
        return self.module.ensure_performance_indexes(self.db, self.logger)

    def test_original_startup_and_idempotence(self):
        """原初始化已经补齐四项；缺失库初次创建，再次检查只复用。"""
        names = {row[1] for row in self.exported["schema"] if row[0] == "index"}
        self.assertTrue({spec.name for spec in self.module.PERFORMANCE_INDEXES} <= names)
        self.assertEqual([item.status for item in self.ensure()], ["created"] * 4)
        self.assertEqual([item.status for item in self.ensure()], ["existing"] * 4)

    def test_existing_equivalent_and_partial_condition(self):
        """异名相同定义被复用，部分索引要求相同条件而非只匹配列。"""
        self.db.execute_sql("CREATE INDEX previous_messages ON messages(chat_id, time)")
        self.db.execute_sql(
            'CREATE INDEX previous_expression ON expression(chat_id) WHERE "checked" AND NOT "rejected"'
        )
        result = self.ensure()
        self.assertEqual(result[0].actual_name, "previous_messages")
        self.assertEqual(result[2].actual_name, "previous_expression")
        self.assertEqual([item.status for item in result], ["existing", "created", "existing", "created"])

    def test_conflict_is_preserved_and_other_indexes_continue(self):
        """错误的同名索引必须报告失败，不能删除后重建或阻止其余项。"""
        self.db.execute_sql("CREATE INDEX idx_messages_chat_time ON messages(time)")
        before = self.db.execute_sql("SELECT sql FROM sqlite_master WHERE name='idx_messages_chat_time'").fetchone()
        result = self.ensure()
        self.assertEqual([item.status for item in result], ["failed", "created", "created", "created"])
        self.assertEqual(
            before, self.db.execute_sql("SELECT sql FROM sqlite_master WHERE name='idx_messages_chat_time'").fetchone()
        )

    def test_incompatible_indexes_are_not_equivalent(self):
        """不同列序、排序规则、方向和部分条件不能被误认为相同索引。"""
        self.db.execute_sql("CREATE INDEX reverse_columns ON messages(time, chat_id)")
        self.db.execute_sql("CREATE INDEX descending_time ON action_records(chat_id, time DESC)")
        self.db.execute_sql("CREATE INDEX different_filter ON expression(chat_id) WHERE checked")
        self.db.execute_sql("CREATE INDEX insensitive_content ON jargon(content COLLATE NOCASE)")
        self.assertEqual([item.status for item in self.ensure()], ["created"] * 4)

    def test_record_and_existing_index_preservation(self):
        """实际完整表中的主键及全部字段保持不变，也不删除既有索引。"""
        columns = self.db.execute_sql("PRAGMA table_info(jargon)").fetchall()
        values = [
            42 if row[1] == "id" else (0 if row[2] in ("INTEGER", "REAL", "FLOAT", "DOUBLE") else "合成资料")
            for row in columns
        ]
        self.db.execute_sql(f"INSERT INTO jargon VALUES ({','.join('?' for _ in values)})", values)
        before = self.db.execute_sql("SELECT * FROM jargon").fetchall()
        indexes = set(self.db.execute_sql("SELECT name FROM sqlite_master WHERE type='index'").fetchall())
        statements = []
        self.db.connection().set_trace_callback(statements.append)
        self.ensure()
        self.db.connection().set_trace_callback(None)
        self.assertEqual(before, self.db.execute_sql("SELECT * FROM jargon").fetchall())
        self.assertTrue(
            indexes <= set(self.db.execute_sql("SELECT name FROM sqlite_master WHERE type='index'").fetchall())
        )
        self.assertFalse(
            any(sql.lstrip().upper().startswith(("DELETE", "DROP", "VACUUM", "ALTER", "UPDATE")) for sql in statements)
        )

    def test_failure_rolls_back_one_index(self):
        """模拟创建语句执行后异常，确认该项回滚而其他项正常提交。"""
        execute = self.db.execute_sql

        def fail_after_create(sql, *args, **kwargs):
            """只在一个索引创建后注入磁盘故障，保留真实 SQLite 事务行为。"""
            result = execute(sql, *args, **kwargs)
            if sql.startswith('CREATE INDEX "idx_action_records_chat_time"'):
                raise OperationalError("合成磁盘故障")
            return result

        with patch.object(self.db, "execute_sql", side_effect=fail_after_create):
            results = self.ensure()
        self.assertEqual([item.status for item in results], ["created", "failed", "created", "created"])
        self.assertIsNone(
            execute("SELECT name FROM sqlite_master WHERE name='idx_action_records_chat_time'").fetchone()
        )
        self.assertEqual([item.status for item in self.ensure()], ["existing", "created", "existing", "existing"])

    def test_locked_database_recovers_on_next_check(self):
        """另一连接持有写锁时有界失败，释放后下一次检查成功。"""
        path = Path(self.directory.name) / "locked.db"
        writer = SqliteDatabase(path, timeout=0.03)
        writer.connect()
        self.create_schema(writer)
        reader = SqliteDatabase(path, timeout=0.03)
        reader.connect()
        try:
            writer.execute_sql("BEGIN IMMEDIATE")
            start = time.monotonic()
            results = self.module.ensure_performance_indexes(reader, self.logger)
            self.assertEqual([item.status for item in results], ["failed"] * 4)
            self.assertLess(time.monotonic() - start, 2)
            writer.execute_sql("ROLLBACK")
            self.assertEqual(
                [item.status for item in self.module.ensure_performance_indexes(reader, self.logger)], ["created"] * 4
            )
        finally:
            reader.close()
            writer.close()

    def test_read_only_database_is_reported(self):
        """只读库无法补建时仍返回完整失败结果，不把缺失索引记作就绪。"""
        path = Path(self.directory.name) / "readonly.db"
        writer = SqliteDatabase(path)
        with writer.connection_context():
            self.create_schema(writer)
        reader = SqliteDatabase(f"file:{path.as_posix()}?mode=ro", uri=True)
        with reader.connection_context():
            self.assertEqual(
                [item.status for item in self.module.ensure_performance_indexes(reader, self.logger)], ["failed"] * 4
            )


def benchmark(output: Path) -> None:
    """以完整实际模型结构比较四类查询，并保存查询计划和重复测量结果。"""
    with tempfile.TemporaryDirectory(prefix="maibot-index-benchmark-") as directory:
        exported = isolated_schema(Path(directory))
        sys.path.insert(0, str(ROOT))
        from src.common.database.performance_indexes import PERFORMANCE_INDEXES, ensure_performance_indexes

        db = SqliteDatabase(":memory:")
        db.connect()
        names = {spec.name for spec in PERFORMANCE_INDEXES}
        for kind, name, sql in exported["schema"]:
            if kind == "table" or (kind == "index" and name not in names):
                db.execute_sql(sql)
        tables = ("messages", "action_records", "expression", "jargon")
        for table in tables:
            columns = db.execute_sql(f'PRAGMA table_info("{table}")').fetchall()

            def rows(table=table, columns=columns):
                """生成有实际列类型与约束的合成记录，审核分布独立于会话编号。"""
                for number in range(30000 if table == "jargon" else 100000):
                    values = {
                        "id": number + 1,
                        "message_id": f"msg{number}",
                        "action_id": f"act{number}",
                        "chat_id": f"c{number % 100}",
                        "time": float(number),
                        "user_id": "940000001",
                        "intercept_message_level": 0,
                        "count": number % 10 + 1,
                        "checked": int((number // 100) % 100 < 2),
                        "rejected": int((number // 100) % 7 == 0),
                        "style": "合成表达" * 20,
                        "content": f"term{number}",
                        "meaning": f"meaning{number % 50}",
                    }
                    yield tuple(
                        values.get(row[1], 0 if row[2] in ("INTEGER", "REAL", "FLOAT", "DOUBLE") else "合成资料")
                        for row in columns
                    )

            with db.atomic():
                db.connection().executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in columns)})', rows())
        records = []
        expected = []
        for variant in ("baseline", "indexed"):
            if variant == "indexed":
                results = ensure_performance_indexes(db, logging.getLogger("benchmark"))
                assert all(result.status == "created" for result in results)
            for index, (sql, parameters) in enumerate(exported["queries"]):
                for _ in range(2):
                    result = db.execute_sql(sql, parameters).fetchall()
                canonical = result if index < 2 else sorted(result)
                if variant == "baseline":
                    expected.append(canonical)
                else:
                    assert canonical == expected[index], tables[index]
                timings = []
                for _ in range(7):
                    start = time.perf_counter()
                    db.execute_sql(sql, parameters).fetchall()
                    timings.append(1000 * (time.perf_counter() - start))
                plan = db.execute_sql("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
                if variant == "indexed":
                    assert any(PERFORMANCE_INDEXES[index].name in row[3] for row in plan), plan
                    if index < 2:
                        assert not any("TEMP B-TREE" in row[3] for row in plan), plan
                records.append(
                    {
                        "table": tables[index],
                        "variant": variant,
                        "rows": len(result),
                        "median_ms": statistics.median(timings),
                        "plan": plan,
                    }
                )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps({"sqlite": sqlite3.sqlite_version, "records": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        db.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--export-schema":
        export_schema(Path(sys.argv[2]), Path(sys.argv[3]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--benchmark":
        benchmark(Path(sys.argv[2]))
    else:
        unittest.main()
