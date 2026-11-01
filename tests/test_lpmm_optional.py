"""在独立源码和依赖环境中验证可选知识库，不读取用户配置或业务数据。"""

import importlib
import importlib.metadata
import json
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
WITH_LPMM = "--with-lpmm" in sys.argv
PACKAGES = ("faiss-cpu", "quick-algo", "pandas", "pyarrow")
MODULES = ("faiss", "quick_algo", "pandas", "pyarrow")


class IsolatedLPMMTests(unittest.TestCase):
    """跨平台复制实际源码后执行子进程，用显式 profile 区分基础和完整安装。"""

    def test_isolated_contracts(self):
        """当前解释器必须对应指定依赖集合；测试配置、数据库和日志全部在临时副本。"""
        with tempfile.TemporaryDirectory(prefix="maibot-lpmm-") as directory:
            source = Path(directory) / "source"
            for name in ("src", "template", "scripts"):
                shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
            (source / "config").mkdir()
            for name in ("bot_config", "model_config"):
                shutil.copyfile(source / "template" / f"{name}_template.toml", source / "config" / f"{name}.toml")
            (source / "data").mkdir()
            (source / "data/webui.json").write_text(
                json.dumps(
                    {
                        "access_token": "lpmm-synthetic-test-token",
                        "first_setup_completed": True,
                    }
                ),
                encoding="utf-8",
            )
            args = [sys.executable, str(Path(__file__).resolve()), "--isolated-source", str(source)]
            if WITH_LPMM:
                args.append("--with-lpmm")
            subprocess.run(args, cwd=directory, env={**os.environ, "PYTHONNOUSERSITE": "1"}, check=True, timeout=180)
            result = json.loads((source / "lpmm-tests.completed").read_text(encoding="utf-8"))
            self.assertGreaterEqual(result["tests"], 10)
            self.assertEqual(result["skipped"], 0)


@unittest.skipUnless(IS_CHILD, "只能在隔离副本导入实际业务模块")
class LPMMContracts(unittest.IsolatedAsyncioTestCase):
    """基础和完整安装均应满足关闭功能、错误分类和认证契约。"""

    @classmethod
    def setUpClass(cls):
        """核对依赖安装状态并建立不允许网络请求的隔离业务环境。"""
        source = Path(sys.argv[sys.argv.index("--isolated-source") + 1]).resolve()
        if not source.parent.name.startswith("maibot-lpmm-"):
            raise RuntimeError("测试源码必须位于专用临时目录")
        os.chdir(source)
        os.environ.update(HOST="127.0.0.1", PORT="0")
        sys.path.insert(0, str(source))
        sys.path.insert(0, str(source / "scripts"))
        for name in PACKAGES:
            if WITH_LPMM:
                importlib.metadata.version(name)
            else:
                with cls().assertRaises(importlib.metadata.PackageNotFoundError):
                    importlib.metadata.version(name)
        original_connect = socket.socket.connect

        def connect(sock, address):
            """Windows 事件循环使用回环 socketpair，允许回环连接但拒绝外部服务。"""
            if not isinstance(address, tuple) or address[0] not in ("127.0.0.1", "::1", "localhost"):
                raise AssertionError("测试禁止外部连接")
            return original_connect(sock, address)

        guard = patch.object(socket.socket, "connect", new=connect)
        guard.start()
        cls.addClassCleanup(guard.stop)
        from src.chat import knowledge
        from src.chat.knowledge import dependencies

        cls.knowledge = knowledge
        cls.dependencies = dependencies

    async def asyncSetUp(self):
        """每例恢复开关、管理器及错误状态，避免依赖检查互相污染。"""
        self.enterContext(patch.object(self.knowledge.global_config.lpmm_knowledge, "enable", False))
        self.enterContext(patch.object(self.knowledge, "qa_manager", None))
        self.enterContext(patch.object(self.knowledge, "_dependency_error", None))

    async def test_00_core_imports_do_not_load_optional_packages(self):
        """导入实际 MainSystem、检索工具和文本脚本不应触发任何知识库原生包。"""
        importlib.import_module("src.main")
        importlib.import_module("src.memory_system.retrieval_tools")
        importlib.import_module("src.chat.knowledge.open_ie")
        importlib.import_module("raw_data_preprocessor")
        importlib.import_module("lpmm_manager")
        self.assertFalse(set(MODULES) & sys.modules.keys())

    async def test_disabled_does_not_check_or_initialize(self):
        """关闭知识库时不检查可选依赖，不初始化管理器。"""
        with patch.object(self.knowledge, "require_lpmm_dependencies", side_effect=AssertionError("不应加载")):
            self.knowledge.lpmm_start_up()
        self.assertIsNone(self.knowledge.get_qa_manager())
        self.assertIsNone(self.knowledge.get_lpmm_dependency_error())

    async def test_dependency_load_error_does_not_change_config(self):
        """真实加载边界中的动态库错误被记录，配置继续保持开启且没有半初始化管理器。"""
        error = OSError("synthetic DLL failure at /private/path")
        self.knowledge.global_config.lpmm_knowledge.enable = True
        with patch.object(self.dependencies, "import_module", side_effect=error):
            self.knowledge.lpmm_start_up()
        self.assertTrue(self.knowledge.global_config.lpmm_knowledge.enable)
        self.assertIsNone(self.knowledge.get_qa_manager())
        self.assertIn("faiss-cpu", self.knowledge.get_lpmm_dependency_error())
        self.assertNotIn("/private/path", self.knowledge.get_lpmm_dependency_error())

    async def test_exception_preserves_original_cause(self):
        """错误提示不泄露路径，但异常链保留实际导入失败原因。"""
        cause = ImportError("synthetic ABI mismatch")
        with patch.object(self.dependencies, "import_module", side_effect=cause):
            with self.assertRaises(self.dependencies.LPMMDependencyError) as captured:
                self.dependencies.require_lpmm_dependencies()
        self.assertIs(captured.exception.__cause__, cause)
        self.assertIn("--extra lpmm", str(captured.exception))

    async def test_query_distinguishes_disabled_and_unavailable(self):
        """查询工具区分关闭和缺依赖，不把缺依赖当成没有检索命中。"""
        from src.memory_system.retrieval_tools.query_lpmm_knowledge import query_lpmm_knowledge

        self.assertEqual(await query_lpmm_knowledge("合成问题"), "LPMM知识库未启用")
        self.knowledge.global_config.lpmm_knowledge.enable = True
        with patch.object(self.dependencies, "import_module", side_effect=ImportError("synthetic")):
            self.knowledge.lpmm_start_up()
        answer = await query_lpmm_knowledge("合成问题")
        self.assertIn("不可用", answer)
        self.assertIn("--extra lpmm", answer)
        self.assertNotIn("未找到与", answer)

    async def test_http_auth_and_dependency_errors(self):
        """实际后端保持认证，已认证请求获得 503，普通健康接口仍可用。"""
        import httpx
        from src.webui.webui_server import WebUIServer

        server = WebUIServer("127.0.0.1", 0)
        self.addAsyncCleanup(server.frontend.close)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(server.app), base_url="http://test") as client:
            for path in ("graph", "stats", "search?query=synthetic"):
                self.assertEqual((await client.get(f"/api/webui/knowledge/{path}")).status_code, 401)
            headers = {"Authorization": "Bearer lpmm-synthetic-test-token"}
            with patch.object(self.dependencies, "import_module", side_effect=OSError("secret local path")):
                for path in ("graph", "stats", "search?query=synthetic"):
                    response = await client.get(f"/api/webui/knowledge/{path}", headers=headers)
                    self.assertEqual(response.status_code, 503)
                    self.assertIn("--extra lpmm", response.json()["detail"])
                    self.assertNotIn("secret local path", response.text)
            self.assertEqual((await client.get("/api/webui/health")).status_code, 200)

    async def test_help_loads_without_backend_initialization(self):
        """帮助和纯文本入口不要求知识库依赖；不执行任何管理操作。"""
        for name in ("lpmm_manager", "import_openie", "delete_lpmm_items", "inspect_lpmm_batch", "test_lpmm_retrieval"):
            module = importlib.import_module(name)
            with self.subTest(script=name), patch.object(sys, "argv", [name, "--help"]):
                with self.assertRaises(SystemExit) as captured:
                    module.main()
                self.assertEqual(captured.exception.code, 0)
        if not WITH_LPMM:
            self.assertFalse(set(MODULES) & sys.modules.keys())

    async def test_cli_fails_before_creating_vector_or_graph_data(self):
        """命令检查失败使用非零退出，不进入导入确认或创建知识库目录。"""
        import import_openie

        with patch.object(self.dependencies, "import_module", side_effect=ImportError("synthetic")):
            with self.assertRaises(SystemExit) as captured:
                await import_openie.main_async(non_interactive=True)
        self.assertIn("--extra lpmm", str(captured.exception))
        self.assertFalse(Path("data/embedding").exists())
        self.assertFalse(Path("data/rag").exists())


@unittest.skipUnless(IS_CHILD, "只能在隔离副本导入实际业务模块")
class BaseEnvironmentContracts(LPMMContracts):
    """默认安装真实缺少四项依赖，不能用伪造模块替代成功条件。"""

    async def test_real_missing_dependencies(self):
        """实际缺包时启动知识库返回错误，普通主系统仍可构造。"""
        self.knowledge.global_config.lpmm_knowledge.enable = True
        self.knowledge.lpmm_start_up()
        self.assertIsNone(self.knowledge.get_qa_manager())
        self.assertIn("faiss-cpu", self.knowledge.get_lpmm_dependency_error())
        from src.main import MainSystem

        with patch.object(self.knowledge.global_config.webui, "enabled", False):
            self.assertIsNotNone(MainSystem())

    async def test_text_preprocessing_without_optional_packages(self):
        """文本预处理与 OpenIE 数据结构只使用默认依赖。"""
        from src.chat.knowledge.open_ie import OpenIE
        import raw_data_preprocessor

        self.assertIsNotNone(OpenIE)
        directory = Path(raw_data_preprocessor.RAW_DATA_PATH)
        directory.mkdir(parents=True)
        (directory / "synthetic.txt").write_text("合成文段甲\n\n合成文段乙\n", encoding="utf-8")
        hashes, paragraphs = raw_data_preprocessor.load_raw_data()
        self.assertEqual(len(hashes), 2)
        self.assertEqual(paragraphs, ["合成文段甲", "合成文段乙"])
        self.assertFalse(set(MODULES) & sys.modules.keys())


@unittest.skipUnless(IS_CHILD, "只能在隔离副本导入实际业务模块")
class FullEnvironmentContracts(LPMMContracts):
    """完整安装使用实际向量索引、Parquet 和图扩展验证读写。"""

    async def test_partial_dependencies_report_correct_package(self):
        """在真实完整环境逐项注入导入失败，其他包继续使用真实实现。"""
        original = self.dependencies.import_module
        for module, package in (
            ("faiss", "faiss-cpu"),
            ("quick_algo.pagerank", "quick-algo"),
            ("pandas", "pandas"),
            ("pyarrow", "pyarrow"),
        ):

            def importing(name, failed_module=module):
                """仅令指定依赖导入失败，不安装任何替代模块。"""
                if name == failed_module:
                    raise ModuleNotFoundError(f"synthetic missing {failed_module}")
                return original(name)

            with self.subTest(package=package), patch.object(self.dependencies, "import_module", side_effect=importing):
                with self.assertRaises(self.dependencies.LPMMDependencyError) as captured:
                    self.dependencies.require_lpmm_dependencies()
                self.assertEqual(captured.exception.package, package)

    async def test_vector_and_parquet_roundtrip(self):
        """真实索引保存后读回，相同合成向量仍命中相同文段。"""
        self.dependencies.require_lpmm_dependencies()
        from src.chat.knowledge.embedding_store import EmbeddingStore, EmbeddingStoreItem

        with (
            tempfile.TemporaryDirectory(dir=Path.cwd()) as directory,
            patch.object(self.knowledge.global_config.lpmm_knowledge, "embedding_dimension", 3),
        ):
            store = EmbeddingStore("synthetic", directory)
            store.store = {
                "first": EmbeddingStoreItem("first", [1.0, 0.0, 0.0], "合成文段甲"),
                "second": EmbeddingStoreItem("second", [0.0, 1.0, 0.0], "合成文段乙"),
            }
            store.build_faiss_index()
            expected = store.search_top_k([1.0, 0.0, 0.0], 1)
            self.assertEqual(expected[0][0], "first")
            store.save_to_file()
            restored = EmbeddingStore("synthetic", directory)
            restored.load_from_file()
            self.assertEqual(restored.search_top_k([1.0, 0.0, 0.0], 1), expected)
            self.assertEqual(restored.store["first"].str, "合成文段甲")

    async def test_empty_knowledge_and_disabled_graph_access(self):
        """完整依赖下空库正常返回空结果，聊天开关关闭也不禁止手动查看。"""
        import httpx
        from fastapi import FastAPI
        from src.webui.knowledge_routes import router

        application = FastAPI()
        application.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url="http://test") as client:
            headers = {"Authorization": "Bearer lpmm-synthetic-test-token"}
            for path in ("graph", "stats", "search?query=synthetic"):
                self.assertEqual((await client.get(f"/api/webui/knowledge/{path}", headers=headers)).status_code, 200)

    async def test_enabled_initializes_real_managers(self):
        """空库启动沿用原有加载流程，不请求嵌入模型。"""
        self.knowledge.global_config.lpmm_knowledge.enable = True
        self.knowledge.lpmm_start_up()
        self.assertIsNotNone(self.knowledge.get_qa_manager())
        self.assertIsNone(self.knowledge.get_lpmm_dependency_error())

    async def test_graph_parquet_pagerank_and_http_roundtrip(self):
        """实际图与计数读回后可运行 PageRank，并供关闭聊天知识库时的 WebUI 查询。"""
        import httpx
        from fastapi import FastAPI
        from quick_algo import di_graph, pagerank
        from src.chat.knowledge import kg_manager
        from src.webui.knowledge_routes import router

        with (
            tempfile.TemporaryDirectory(dir=Path.cwd()) as directory,
            patch.object(kg_manager, "get_kg_dir_str", return_value=directory),
        ):
            manager = kg_manager.KGManager()
            manager.graph.add_edge(di_graph.DiEdge("entity-synthetic", "paragraph-synthetic", {"weight": 1.0}))
            for identifier, kind in (("entity-synthetic", "ent"), ("paragraph-synthetic", "pg")):
                node = manager.graph[identifier]
                node["content"] = f"合成知识 {identifier}"
                node["type"] = kind
                manager.graph.update_node(node)
            manager.ent_appear_cnt = {"entity-synthetic": 2}
            manager.stored_paragraph_hashes = {"synthetic"}
            manager.save_to_file()
            restored = kg_manager.KGManager()
            restored.load_from_file()
            self.assertEqual(restored.ent_appear_cnt, manager.ent_appear_cnt)
            self.assertEqual(restored.stored_paragraph_hashes, manager.stored_paragraph_hashes)
            scores = pagerank.run_pagerank(
                restored.graph, personalization={"entity-synthetic": 1.0}, max_iter=100, alpha=0.8
            )
            self.assertEqual(set(scores), {"entity-synthetic", "paragraph-synthetic"})
            self.assertAlmostEqual(sum(scores.values()), 1.0, places=5)
            application = FastAPI()
            application.include_router(router)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(application), base_url="http://test") as client:
                headers = {"Authorization": "Bearer lpmm-synthetic-test-token"}
                graph = await client.get("/api/webui/knowledge/graph", headers=headers)
                self.assertEqual(graph.status_code, 200)
                self.assertEqual(len(graph.json()["nodes"]), 2)
                self.assertEqual(len(graph.json()["edges"]), 1)
                stats = await client.get("/api/webui/knowledge/stats", headers=headers)
                self.assertEqual(stats.json()["total_nodes"], 2)
                search = await client.get("/api/webui/knowledge/search?query=entity-synthetic", headers=headers)
                self.assertEqual([node["id"] for node in search.json()], ["entity-synthetic"])


if __name__ == "__main__":
    case = (FullEnvironmentContracts if WITH_LPMM else BaseEnvironmentContracts) if IS_CHILD else IsolatedLPMMTests
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(case))
    if IS_CHILD and result.wasSuccessful() and not result.skipped:
        Path("lpmm-tests.completed").write_text(
            json.dumps({"tests": result.testsRun, "skipped": len(result.skipped)}), encoding="utf-8"
        )
    raise SystemExit(0 if result.wasSuccessful() and not result.skipped else 1)
