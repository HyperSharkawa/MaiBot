"""仅在使用知识库时检查可选依赖，不在导入本模块时加载原生扩展。"""

from importlib import import_module


INSTALL_COMMAND = "uv sync --locked --no-dev --extra lpmm"
UNAVAILABLE_MESSAGE = f"LPMM 依赖未安装或无法加载，请运行 {INSTALL_COMMAND}，修复依赖后重启。"


class LPMMDependencyError(RuntimeError):
    """标识可选依赖加载失败，与知识库数据和模型调用错误区分。"""

    def __init__(self, package: str):
        """保存失败包名；公开错误不包含本地路径或底层堆栈。"""
        self.package = package
        super().__init__(f"{package} 不可用。{UNAVAILABLE_MESSAGE}")


def require_lpmm_dependencies() -> None:
    """实际加载四项依赖及图扩展；只转换第三方模块加载阶段的异常。"""
    for package, modules in (
        ("faiss-cpu", ("faiss",)),
        ("quick-algo", ("quick_algo.di_graph", "quick_algo.pagerank")),
        ("pandas", ("pandas",)),
        ("pyarrow", ("pyarrow",)),
    ):
        try:
            for module in modules:
                import_module(module)
        except Exception as error:
            # 原生扩展还可能因 ABI 或动态库错误而无法加载，不能仅检查包是否存在。
            raise LPMMDependencyError(package) from error


def require_lpmm_for_cli() -> None:
    """在知识库命令开始操作数据前检查依赖，失败时输出安装提示并非零退出。"""
    try:
        require_lpmm_dependencies()
    except LPMMDependencyError as error:
        from src.chat.knowledge.global_logger import logger

        logger.error("%s 原因：%s", error, error.__cause__)
        raise SystemExit(str(error)) from error
