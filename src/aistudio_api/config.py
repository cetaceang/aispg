"""Runtime configuration helpers."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件（如果存在）
load_dotenv()

DEFAULT_TEXT_MODEL = os.getenv("AISTUDIO_DEFAULT_TEXT_MODEL", "gemini-3.7-flash")


def _load_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return None


def _load_bool_env(*names: str, default: bool) -> bool:
    value = _load_env(*names)
    if value is None:
        return default
    return value not in ("0", "false", "False")


def _load_web_password() -> str | None:
    return os.getenv("AISTUDIO_WEB_PASSWORD") or os.getenv("AISTUDIO_ADMIN_PASSWORD")


def _load_dump_requests_env() -> bool:
    """检查 DEBUG / AISTUDIO_DEBUG 环境变量是否启用请求转储。

    注意：此环境变量专用于触发请求详细信息转储 (Dump)，不影响日志输出级别。
    """
    val = _load_env("DEBUG", "AISTUDIO_DEBUG", "AISTUDIO_DUMP_REQUESTS")
    if val is None:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on", "dump", "y", "t")


def _load_dump_to_file_env() -> bool:
    """检查 AISTUDIO_DUMP_FILE / AISTUDIO_DUMP_TO_FILE 环境变量是否启用将请求转储持久化保存到文件。"""
    val = _load_env("AISTUDIO_DUMP_FILE", "AISTUDIO_DUMP_TO_FILE")
    if val is None:
        return False
    return val.strip().lower() in ("1", "true", "yes", "on", "dump", "y", "t")


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "data"
_DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_TEMPLATE_CONFIG_PATHS = (
    _PROJECT_ROOT / "defaults" / "config.yaml",
    _DEFAULT_CONFIG_PATH,
)


def resolve_config_file(config_path: str | os.PathLike[str] | None = None) -> Path:
    """解析配置文件路径（优先使用 AISTUDIO_CONFIG_FILE 环境变量）。

    若目标路径配置文件不存在，则自动从内置配置模板初始化复制，绝不覆盖已有配置文件。
    """
    if config_path is not None:
        target = Path(config_path)
    else:
        override = os.getenv("AISTUDIO_CONFIG_FILE")
        target = Path(override) if override else _DEFAULT_CONFIG_PATH

    if not target.exists():
        for template in _TEMPLATE_CONFIG_PATHS:
            if template.is_file():
                try:
                    if template.resolve() == target.resolve():
                        break
                except OSError:
                    pass
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(template, target)
                    break
                except OSError:
                    pass
    return target


def resolve_data_dir() -> Path:
    """解析数据存储根目录（优先使用 AISTUDIO_DATA_DIR 环境变量）。"""
    override = os.getenv("AISTUDIO_DATA_DIR")
    if override:
        path = Path(override).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    _DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return _DEFAULT_DATA_DIR.resolve()


def resolve_stats_file() -> Path:
    """解析调用量与 Token 统计持久化文件路径（优先使用 AISTUDIO_STATS_FILE）。"""
    override = os.getenv("AISTUDIO_STATS_FILE")
    if override:
        path = Path(override).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return resolve_data_dir() / "stats.json"


def resolve_rotator_state_file() -> Path:
    """解析账号调度与 429 冷却状态持久化文件路径（优先使用 AISTUDIO_ROTATOR_STATE_FILE）。"""
    override = os.getenv("AISTUDIO_ROTATOR_STATE_FILE")
    if override:
        path = Path(override).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return resolve_data_dir() / "rotator_state.json"


_AUTH_SEARCH_ROOTS = [
    _DEFAULT_DATA_DIR,  # 项目内 data/ 目录
]


def discover_auth_file() -> str | None:
    override = os.getenv("AISTUDIO_AUTH_FILE")
    if override:
        return override

    accounts_override = os.getenv("AISTUDIO_ACCOUNTS_DIR")
    data_override = os.getenv("AISTUDIO_DATA_DIR")
    accounts_dirs = (
        [Path(accounts_override)]
        if accounts_override
        else [Path(data_override) / "accounts"]
        if data_override
        else [root / "accounts" for root in _AUTH_SEARCH_ROOTS]
    )
    for accounts_dir in accounts_dirs:
        if not accounts_dir.is_dir():
            continue
        # 优先从 registry.json 读取活跃账号
        registry_path = accounts_dir / "registry.json"
        if registry_path.exists():
            try:
                registry = json.loads(registry_path.read_text())
                active_id = registry.get("active_account_id")
                if active_id:
                    auth_path = accounts_dir / active_id / "auth.json"
                    if auth_path.exists():
                        return str(auth_path)
            except (json.JSONDecodeError, KeyError):
                pass
    return None


def discover_proxy_url() -> str | None:
    # Explicit application override wins. Ambient HTTP(S)_PROXY must not
    # accidentally bypass our default WARP egress.
    explicit = os.getenv("AISTUDIO_PROXY", "").strip()
    if explicit:
        return explicit
    if _load_bool_env("AISTUDIO_WARP_ENABLED", default=True):
        port = int(os.getenv("AISTUDIO_WARP_PORT", "1080"))
        if not 1 <= port <= 65535:
            raise ValueError("AISTUDIO_WARP_PORT must be between 1 and 65535")
        return f"socks5://127.0.0.1:{port}"
    env_proxy = (
        os.getenv("HTTPS_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("HTTP_PROXY")
        or os.getenv("http_proxy")
    )
    if env_proxy:
        return env_proxy
    return None


def resolve_timezone() -> str:
    """解析系统或配置时区（优先使用 AISTUDIO_TIMEZONE 环境变量）。"""
    override = os.getenv("AISTUDIO_TIMEZONE")
    if override and override.strip():
        return override.strip()
    try:
        from datetime import datetime

        tz = datetime.now().astimezone().tzinfo
        if tz:
            tz_str = str(tz)
            if "/" in tz_str:
                return tz_str
    except Exception:
        pass
    return "America/Los_Angeles"


@dataclass(slots=True)
class Settings:
    port: int = int(os.getenv("AISTUDIO_PORT", "8080"))
    proof_server_url: str = os.getenv(
        "AISTUDIO_PROOF_SERVER_URL", "https://waa.spinsnow.fun"
    )
    auth_file: str | None = discover_auth_file()
    tmp_dir: str = os.getenv("AISTUDIO_TMP_DIR", tempfile.gettempdir())
    proxy_url: str | None = discover_proxy_url()
    warp_enabled: bool = _load_bool_env("AISTUDIO_WARP_ENABLED", default=True)
    warp_port: int = int(os.getenv("AISTUDIO_WARP_PORT", "1080"))
    warp_dir: str = _load_env("AISTUDIO_WARP_DIR") or str(resolve_data_dir() / "warp")
    warp_profile: str = os.getenv("AISTUDIO_WARP_PROFILE", "")
    warp_endpoint: str = os.getenv("AISTUDIO_WARP_ENDPOINT", "")
    warp_start_timeout: float = float(os.getenv("AISTUDIO_WARP_START_TIMEOUT", "60"))
    web_password: str | None = _load_web_password()
    api_keys: frozenset[str] = frozenset()
    timezone: str = resolve_timezone()
    data_dir: str = str(resolve_data_dir())
    accounts_dir: str = os.getenv("AISTUDIO_ACCOUNTS_DIR", "")
    stats_file: str = str(resolve_stats_file())
    rotator_state_file: str = str(resolve_rotator_state_file())
    config_file: str = str(resolve_config_file())
    persist_stats: bool = _load_bool_env("AISTUDIO_PERSIST_STATS", default=True)
    persist_rotator: bool = _load_bool_env("AISTUDIO_PERSIST_ROTATOR", default=True)
    dump_requests: bool = _load_dump_requests_env()
    dump_to_file: bool = _load_dump_to_file_env()
    dump_dir: str = os.getenv("AISTUDIO_DUMP_DIR", str(_PROJECT_ROOT / "dumps"))
    log_level: str = "INFO"

    @property
    def auth_enabled(self) -> bool:
        """网页管理端鉴权是否开启。"""
        return bool(self.web_password)


settings = Settings()
