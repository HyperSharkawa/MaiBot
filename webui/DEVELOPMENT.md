# 修改和构建前端

开发环境使用 Node 24.22.1、Bun 1.4.4 和 Python 3.15。前端工具链版本记录在 `toolchain.json`，依赖固定在 `bun.lock`。

在仓库根目录执行：

```bash
uv run --no-project --python 3.15.2 python scripts/build_webui.py build
```

命令执行冻结安装、TypeScript 检查、Vite 构建与打包，输出到 `webui/.build/`。在没有 `.git` 的源码副本中，另传 `--source-commit <完整提交ID>`。

启动开发服务器：

```bash
cd webui
bun install --frozen-lockfile --ignore-scripts
bun run dev
```

开发页面位于 `http://localhost:7999`，`/api` 请求代理到本机 8001 端口，需要同时启动 MaiBot 的 WebUI 后端。

构建标识由前端源码、资源、锁文件、工具链和构建契约决定。换行统一后计算文本输入，二进制保持原样；仅修改后端不会要求重新发布页面。修改构建规则时须同步 `frontend_contract.py` 的输入清单。

## 自动构建和发行

`.github/workflows/webui.yml` 在 PR 中只检查和构建；主分支相关修改通过后发布 `webui-<构建标识>`，附带 `webui.zip` 与 `webui.zip.sha256`。手动运行工作流可补发默认分支缺失的资源。

构建作业只读，发布作业使用 `GITHUB_TOKEN` 的 `contents: write` 权限。管理员需允许此仓库运行 Actions 和发布 Releases；不需要填写 PAT。已有完整资源经校验后复用，同名冲突或不完整草稿会报错，需要人工检查，不自动覆盖。资源发行版不设置为应用的 Latest Release。

## 本地验证

纯资源与恢复接口测试使用临时目录，不读取业务配置：

```bash
uv run --no-project --python 3.15.2 --with aiohttp==3.14.5 --with fastapi==0.135.3 --with httpx==0.28.1 python -m unittest discover -s tests -p 'test_frontend*.py'
```

## 构建 Docker 镜像

在仓库根目录放置 LPMM 源码目录 `MaiMBot-LPMM/`，然后运行：

```bash
docker build --platform linux/amd64 --build-arg WEBUI_SOURCE_COMMIT="$(git rev-parse HEAD)" -t maibot:local .
```

前端阶段使用同一份工具链和构建脚本；最终镜像只复制静态资源，运行时没有 Node、Bun 或前端依赖目录。配置、数据、日志和虚拟环境由 `.dockerignore` 排除，不应包含在构建上下文内。
