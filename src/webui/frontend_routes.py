"""宿主提供的页面恢复接口；即使 React 资源缺失也可登录并重试。"""

import mimetypes
from html import escape
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from .frontend_assets import DownloadBusyError, FrontendAssets

HOST_FILES = Path(__file__).parent / "frontend_host"
NO_CACHE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow, noarchive"}
RECOVERY_SCRIPT = '<script src="/api/webui/frontend/recovery.js" data-page-version="{version}" defer></script>'


class RetryRequest(BaseModel):
    """只接受服务器提供的源标识，旧客户端不传请求体时仍自动选择。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str = "auto"


def register_frontend_routes(app: FastAPI, assets: FrontendAssets, verify_auth) -> None:
    """注册状态、认证重试和静态页面；必须在业务 API 之后注册。"""
    mimetypes.add_type("application/javascript", ".js")
    mimetypes.add_type("text/css", ".css")

    @app.get("/api/webui/frontend/status")
    async def status():
        """只读取当前下载状态，查询本身不会触发重试。"""
        return JSONResponse(assets.status(), headers=NO_CACHE)

    @app.post("/api/webui/frontend/retry", dependencies=[Depends(verify_auth)], status_code=202)
    async def retry(request: RetryRequest | None = None):
        """已认证用户可重试固定版本，无 URL、路径或版本输入。"""
        try:
            await assets.request_retry(request.source_id if request else "auto")
        except DownloadBusyError as error:
            raise HTTPException(409, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return JSONResponse(assets.status(), status_code=202, headers=NO_CACHE)

    @app.get("/api/webui/frontend/sources", dependencies=[Depends(verify_auth)])
    async def sources():
        """只读返回可选源及自动顺序，不泄露保存的地址或配置内容。"""
        available = await assets.sources()
        return JSONResponse(
            {"sources": [{"id": source.id, "name": source.name} for source in available]}, headers=NO_CACHE
        )

    @app.get("/api/webui/frontend/recovery.js", include_in_schema=False)
    async def recovery_script():
        """独立于下载包提供恢复控件，旧页面也能显示新状态。"""
        return FileResponse(HOST_FILES / "recovery.js", media_type="application/javascript", headers=NO_CACHE)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve(full_path: str):
        """提供当前页面与上一版静态资源；缺失资源不会误返回首页。"""
        if full_path.startswith("api/") or full_path == "api":
            raise HTTPException(404)
        if "\\" in full_path or ":" in full_path or ".." in Path(full_path).parts:
            raise HTTPException(404)
        for directory in (assets.active, assets.previous):
            if directory is None:
                continue
            candidate = (directory / full_path).resolve()
            if not candidate.is_relative_to(directory.resolve()):
                raise HTTPException(404)
            if candidate.is_file() and candidate.suffix != ".html":
                return FileResponse(candidate)
        # 只有无后缀的客户端路由或首页才进入 SPA；缺失脚本、样式返回 404。
        if Path(full_path).suffix and full_path != "index.html":
            raise HTTPException(404)
        if assets.active:
            # 记录这次 HTML 实际使用的版本，后续状态查询可能已经看到更新后的版本。
            version = assets.current_id or "legacy"
            page = (assets.active / "index.html").read_text(encoding="utf-8")
            recovery = RECOVERY_SCRIPT.format(version=escape(version, quote=True))
            page = page.replace("</body>", f"{recovery}</body>")
        else:
            page = (HOST_FILES / "missing.html").read_text(encoding="utf-8")
        return HTMLResponse(page, headers=NO_CACHE)
