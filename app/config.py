"""项目配置：从环境变量加载，支持最小化 .env 解析（仅标准库，无第三方依赖）。

密钥只存在 .env（已 gitignore），绝不写入 .env.example，也绝不打印。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    llm_base_url: str | None
    llm_model: str
    llm_api_key: str
    llm_temperature: float

    @property
    def llm_configured(self) -> bool:
        """是否存在非空项目密钥。"""
        return bool(self.llm_api_key and self.llm_api_key.strip())


def _load_dotenv(path: Path | None = None) -> None:
    """把 .env 里的 KEY=VALUE 读进 os.environ（不覆盖已存在的环境变量）。"""
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip().strip('"').strip("'")
        if key not in os.environ:
            os.environ[key] = value


def load_settings() -> Settings:
    _load_dotenv()
    return Settings(
        llm_provider=os.getenv("LLM_PROVIDER", "deepseek"),
        llm_base_url=os.getenv("LLM_BASE_URL") or None,
        llm_model=os.getenv("LLM_MODEL", "deepseek-flash"),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0") or 0),
    )
