"""只读整理 WebUI 下载源，不初始化插件服务或写入用户配置。"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .frontend_contract import REPOSITORY

GITHUB = "https://github.com"
BUILTINS = (
    ("edgeone", "EdgeOne 加速", "https://edgeone.gh-proxy.com/https://github.com"),
    ("hongkong", "香港加速", "https://hk.gh-proxy.com/https://github.com"),
    ("ghproxy", "gh-proxy 加速", "https://gh-proxy.com/https://github.com"),
)


@dataclass(frozen=True)
class DownloadSource:
    """一次源列表中的下载前缀；公开名称不包含用户保存的地址或凭据。"""

    id: str
    name: str
    prefix: str

    def base_url(self, identity: str) -> str:
        """只生成固定仓库和构建的发行目录，不接受外部目标路径。"""
        return f"{self.prefix}/{REPOSITORY}/releases/download/webui-{identity}"


def normalize_prefix(value: str) -> str:
    """规范化 HTTPS 克隆前缀；拒绝无法安全用于公开文件下载的地址。"""
    if not isinstance(value, str) or any(character.isspace() for character in value) or "\\" in value:
        raise ValueError("下载前缀格式错误")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise ValueError("下载前缀必须使用无认证信息的 HTTPS 地址")
    if parsed.query or parsed.fragment:
        raise ValueError("下载前缀不能包含查询参数或片段")
    port = parsed.port
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None and port != 443:
        host += f":{port}"
    return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))


def read_sources(root: Path, logger: logging.Logger) -> list[DownloadSource]:
    """读取已有镜像设置并补充内置源；缺失或损坏配置不触发文件写入。"""
    mirrors = []
    try:
        data = json.loads((root / "data/webui.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("git_mirrors", []), list):
            raise ValueError("镜像配置结构错误")
        mirrors = data.get("git_mirrors", [])
    except FileNotFoundError:
        pass
    except (ValueError, OSError) as error:
        logger.warning(f"WebUI 镜像配置无法读取（{type(error).__name__}），本次使用内置下载源")

    configured = []
    disabled = set()
    for index, item in enumerate(mirrors, 1):
        try:
            if not isinstance(item, dict) or type(item.get("enabled")) is not bool:
                raise ValueError("镜像启用状态错误")
            prefix = normalize_prefix(item.get("clone_prefix"))
            priority = item.get("priority", 999)
            if type(priority) is not int:
                raise ValueError("镜像优先级错误")
        except ValueError, TypeError:
            logger.warning(f"第 {index} 个镜像不适用于 WebUI 文件下载，已跳过")
            continue
        if not item["enabled"]:
            disabled.add(prefix)
        else:
            configured.append((priority, index, prefix))

    candidates = [DownloadSource("github", "GitHub", GITHUB)]
    # 配置源使用本次列表的临时 ID；地址改变后旧选择必须失效，而不能指向另一个地址。
    candidates.extend(
        DownloadSource(f"configured-{uuid4().hex}", f"自定义加速源 {index}", prefix)
        for _, index, prefix in sorted(configured)
    )
    candidates.extend(DownloadSource(identity, name, prefix) for identity, name, prefix in BUILTINS)
    unique = {}
    for source in candidates:
        if source.prefix not in disabled and source.prefix not in unique:
            unique[source.prefix] = source
    return list(unique.values())
