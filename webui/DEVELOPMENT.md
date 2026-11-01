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

运行时缓存位于 `data/webui/`，仅在启动时尝试一轮，或由已认证用户手动重试。保留当前和上一版资源，不周期重试，也不向“最新版本”回退。可通过标准 `HTTPS_PROXY` / `HTTP_PROXY` 环境变量配置网络代理。

自动下载先尝试 GitHub，再按优先级使用 `data/webui.json` 中已启用的 HTTPS 镜像克隆前缀，最后补充尚未包含的内置加速源。相同地址去重，已明确禁用的地址不会补回。配置只读，不改变插件镜像设置；不支持的地址会跳过。说明页登录后可以为本次重试指定一个源，指定源失败后不会自动换源，下次启动恢复自动模式。

ZIP 下载没有固定总时限：最近 10 秒平均速度持续低于 5 KB/s 达 60 秒，或连续 60 秒未收到数据时，结束当前源。连接限制 10 秒，小型校验文件的请求限制 60 秒。恢复控件显示来源、速度及字节进度；服务器未提供总大小时不显示百分比。全部下载与资源校验通过后才切换页面。

## 本地验证

纯资源与恢复接口测试使用临时目录，不读取业务配置：

```bash
uv run --no-project --python 3.15.2 --with aiohttp==3.14.5 --with fastapi==0.135.3 --with httpx==0.28.1 python -m unittest discover -s tests -p 'test_frontend*.py'
```

后端集成测试使用不含 `dist`、配置、数据和托管缓存的独立源码副本。在副本根目录创建 `.webui-test-fixture` 标记，显式指定资源场景：

```bash
python tests/webui_host_harness.py --prepare --resource-mode bundled --package /path/to/webui.zip
```

`--resource-mode` 支持 `missing`（默认，无资源）、`bundled`（自带资源）、`legacy`（无清单旧页面）和 `cache`（托管缓存）。后三种场景需要与当前源码匹配、已校验的资源包。夹具从模板生成测试配置，监听回环端口 18731，禁止外联，不启动聊天；不要在日常使用的实例目录执行。准备好匹配资源后，副本内可执行 `python -m unittest discover -s tests -p 'test_webui_host_integration.py'` 检查认证和选模。

浏览器测试脚本为 `tests/webui_browser.cjs`，使用独立安装的 Playwright 1.62.1。将 `PLAYWRIGHT_MODULE` 指向 Playwright 模块，`WEBUI_TEST_URL` 指向本地测试服务，`WEBUI_TEST_OUTPUT` 设为截图输出目录，然后运行 `node tests/webui_browser.cjs`。测试会修改测试配置。默认使用本机 Edge，可通过 `PLAYWRIGHT_CHANNEL` 指定 Chromium 通道。

运行 `node tests/webui_recovery_cases.cjs` 可单独检查恢复控件的就绪竞态、登录选源、已知或未知大小的进度、低速提示、按钮状态及页面离开/返回。设置 `WEBUI_TEST_OUTPUT` 可保存桌面和手机截图。完整失败恢复使用另一个干净副本运行 `python tests/webui_recovery_harness.py --package /path/to/webui.zip --resource-mode missing`，再将浏览器脚本的 `WEBUI_RECOVERY_TEST_URL` 指向其回环端口 18732。测试旧页面时，两端分别设置 `--resource-mode legacy` 和 `WEBUI_RECOVERY_RESOURCE_MODE=legacy`。

资源测试包含真实回环 HTTP 换源、持续低速和断流测试，以及一场超过 60 秒的持续下载。完整资源测试因此需要一分多钟。速度阈值和恢复计时的边界使用受控单调时钟检查，不连接公共节点。

Git 更新测试在临时远端与用户副本中进行，不修改当前仓库历史。指定仍含受跟踪产物的历史提交和当前候选匹配的资源包：

```bash
python tests/webui_upgrade_harness.py --before-ref <历史提交> --package /path/to/webui.zip
```

此测试检查实际快进更新、产物删除、本地修改冲突、配置与数据库保留、新 clone 和无 Git 源码运行。下载边界使用指定的已校验包；公开 Release 的实际可下载性应单独检查。

## 构建 Docker 镜像

在仓库根目录放置 LPMM 源码目录 `MaiMBot-LPMM/`，然后运行：

```bash
docker build --platform linux/amd64 --build-arg WEBUI_SOURCE_COMMIT="$(git rev-parse HEAD)" -t maibot:local .
```

前端阶段使用同一份工具链和构建脚本；最终镜像只复制静态资源，运行时没有 Node、Bun 或前端依赖目录。配置、数据、日志和虚拟环境由 `.dockerignore` 排除，不应包含在构建上下文内。
