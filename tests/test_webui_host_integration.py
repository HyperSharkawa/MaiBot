"""在标记过的独立副本中验证宿主认证、禁用状态及真实选模流程。"""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((ROOT / ".webui-test-fixture").is_file(), "需要使用独立 WebUI 测试副本")
class HostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """使用实际配置、认证和调度函数，替换产生网络副作用的模型客户端。"""

    async def test_priority_retries_and_next_request_starts_first(self):
        """首模型重试耗尽后换第二个；下一次仍从首模型开始。"""
        from src.config.config import model_config
        from src.config.api_ada_configs import TaskConfig
        from src.llm_models.exceptions import NetworkConnectionError
        from src.llm_models.utils_model import APIResponse, LLMRequest, RequestType, client_registry

        names = list(model_config.model_task_config.utils.model_list)
        request = LLMRequest(TaskConfig(model_list=names, selection_strategy="priority"))
        provider = model_config.get_provider(model_config.get_model_info(names[0]).api_provider)
        calls = []

        async def response(**kwargs):
            """只模拟网络客户端；保留实际重试和故障切换逻辑。"""
            name = kwargs["model_info"].name
            calls.append(name)
            if name == names[0]:
                raise NetworkConnectionError("synthetic failure")
            return APIResponse(content="synthetic success")

        with (
            patch.object(provider, "max_retry", 2),
            patch.object(provider, "retry_interval", 0),
            patch.object(
                client_registry, "get_client_class_instance", return_value=SimpleNamespace(get_response=response)
            ),
        ):
            for _ in range(2):
                result, model = await request._execute_request(RequestType.RESPONSE)
                self.assertEqual(result.content, "synthetic success")
                self.assertEqual(model.name, names[1])
        self.assertEqual(calls, [names[0], names[0], names[1]] * 2)

    async def test_real_cookie_and_bearer_auth(self):
        """未登录重试被拒，宿主登录产生的 Cookie 与 Bearer 均可调用。"""
        from src.webui.webui_server import WebUIServer

        server = WebUIServer("127.0.0.1", 18739)
        self.addAsyncCleanup(server.frontend.close)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(server.app), base_url="http://127.0.0.1") as client:
            self.assertEqual((await client.post("/api/webui/frontend/retry")).status_code, 401)
            response = await client.post("/api/webui/auth/verify", json={"token": "webui-synthetic-test-token"})
            self.assertTrue(response.json()["valid"])
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            self.assertEqual((await client.post("/api/webui/frontend/retry")).status_code, 202)
            await server.frontend._task
            self.assertEqual(server.frontend.state, "ready")
            client.cookies.clear()
            self.assertEqual(
                (
                    await client.post(
                        "/api/webui/frontend/retry", headers={"Authorization": "Bearer webui-synthetic-test-token"}
                    )
                ).status_code,
                202,
            )
            self.assertEqual((await client.get("/api/webui/health")).status_code, 200)

    async def test_disabled_webui_does_not_create_or_download(self):
        """禁用 WebUI 时仍沿用宿主原入口，不构造服务器和下载任务。"""
        from src.main import MainSystem, global_config

        system = MainSystem.__new__(MainSystem)
        system.webui_server = None
        with (
            patch.object(global_config.webui, "enabled", False),
            patch("src.webui.webui_server.get_webui_server", side_effect=AssertionError("禁用时不应初始化")),
        ):
            system._setup_webui_server()
        self.assertIsNone(system.webui_server)


if __name__ == "__main__":
    unittest.main()
