#!/bin/bash
# 仅管理 MaiBot 的 uv 环境；调用方决定是否执行服务或适配器操作。

require_uv() {
    # 不自动安装全局工具，也不回退到系统 Python。
    if ! command -v uv >/dev/null 2>&1; then
        echo "需要已安装的 uv，请先安装 uv 后重试。" >&2
        return 1
    fi
}

validate_maibot_python() {
    # 已有环境必须明确符合固定版本，避免无提示地替换用户环境。
    local executable=$1
    if [[ ! -x "$executable" ]] || ! "$executable" -c 'import sys; sys.exit(sys.version_info[:3] != (3, 15, 2))'; then
        echo "需要 Python 3.15.2 环境：$executable。请先迁移已有环境。" >&2
        return 1
    fi
}

prepare_maibot_environment() {
    # 保留既有虚拟环境路径；只有路径不存在时才创建新环境。
    local environment=$1
    require_uv || return 1
    if [[ -e "$environment" ]]; then
        validate_maibot_python "$environment/bin/python" || return 1
    else
        uv venv --python 3.15.2 "$environment" || return 1
    fi
}

install_maibot_dependencies() {
    # 使用锁定依赖，但不清除共享环境中适配器已安装的额外包。
    local project=$1
    local executable=$2
    local requirements_file
    local result=0
    shift 2
    require_uv || return 1
    validate_maibot_python "$executable" || return 1
    requirements_file=$(mktemp) || return 1
    if ! uv export --locked --no-dev --extra lpmm --no-emit-project --no-hashes --project "$project" --output-file "$requirements_file" >/dev/null; then
        rm -f -- "$requirements_file"
        return 1
    fi
    # 共享环境中的额外安装也受应用锁约束，避免适配器覆盖已验收的依赖。
    if [[ $# -eq 0 ]]; then
        uv pip install --python "$executable" -r "$requirements_file" || result=$?
    else
        uv pip install --python "$executable" --constraint "$requirements_file" "$@" || result=$?
    fi
    rm -f -- "$requirements_file"
    return "$result"
}
