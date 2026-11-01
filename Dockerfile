# 前端使用同一锁定工具链，最终运行镜像不包含 Node、Bun 或 node_modules。
FROM node:24.22.1-bookworm-slim AS webui-builder
COPY --from=oven/bun:1.4.4 /usr/local/bin/bun /usr/local/bin/bun
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /build
COPY webui ./webui
COPY scripts/build_webui.py ./scripts/build_webui.py
COPY src/__init__.py ./src/__init__.py
COPY src/webui/__init__.py src/webui/frontend_contract.py ./src/webui/
COPY .github/workflows/webui.yml ./.github/workflows/webui.yml
ARG WEBUI_SOURCE_COMMIT
RUN uv run --no-project --python 3.15.2 python scripts/build_webui.py build --source-commit "$WEBUI_SOURCE_COMMIT"

# 编译 LPMM
FROM python:3.15.2-slim AS lpmm-builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /MaiMBot-LPMM

# 同级目录下需要有 MaiMBot-LPMM
COPY MaiMBot-LPMM /MaiMBot-LPMM

# 安装编译器和编译依赖
RUN apt-get update && apt-get install -y build-essential
# 此阶段只构建 quick-algo；应用运行依赖由下一阶段的项目锁统一安装。
RUN uv pip install --system build "Cython>=3" "setuptools>=68" wheel

# 编译 LPMM
RUN cd /MaiMBot-LPMM/lib/quick_algo && python build_lib.py --cleanup --cythonize --build_wheel

# 运行环境
FROM python:3.15.2-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# 工作目录
WORKDIR /MaiMBot

# 复制依赖列表
COPY pyproject.toml uv.lock ./

# 从同一 Python 版本的编译阶段取得扩展，避免复制其他 ABI 的 site-packages。
COPY --from=lpmm-builder /MaiMBot-LPMM/lib/quick_algo/dist/*.whl /tmp/lpmm-wheels/

# pyahocorasick 等依赖需要源码构建；完成后移除运行时不需要的编译器。
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential libgomp1 libstdc++6 \
    && uv export --locked --no-dev --extra lpmm --no-emit-project --no-hashes --output-file /tmp/runtime-requirements.txt \
    && uv pip install --system -r /tmp/runtime-requirements.txt \
    && uv pip install --system --no-deps --reinstall-package quick-algo /tmp/lpmm-wheels/*.whl \
    && uv pip check --system \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/* /tmp/lpmm-wheels /tmp/runtime-requirements.txt

# 复制项目代码
COPY . .
COPY --from=webui-builder /build/webui/.build/dist ./webui/dist

EXPOSE 8000

ENV PYTHON_LAZY_IMPORTS=normal

ENTRYPOINT [ "python","bot.py" ]
