"""Explicit local configuration. Secrets never appear in site output."""
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Settings:
    llm_api_url: str = "https://api.deepseek.com/chat/completions"
    llm_api_key: str = field(default="", repr=False)
    llm_model: str = ""
    easyscholar_key: str = field(default="", repr=False)
    ncbi_api_key: str = field(default="", repr=False)
    ncbi_email: str = field(default="", repr=False)
    openalex_key: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    include_supplements: bool = True
    discovery_limit: int = 20
    discovery_query: str = '(aging OR ageing OR multiomics OR "single cell" OR bioinformatics) AND (software OR tool OR computational OR sequencing) AND (GitHub OR "source code" OR "code availability") AND FIRST_PDATE:[2020-01-01 TO 3000-01-01] AND HAS_FT:Y'
    max_new_per_day: int = 5
    max_ai_calls: int = 5
    max_requests: int = 150
    repair_limit: int = 5
    initialize_existing_first: bool = False
    bootstrap_batch_limit: int = 20
    candidate_checks: int = 8
    max_images: int = 2
    max_supplements: int = 3
    max_file_bytes: int = 6000000
    citation_enrichment: bool = True


ENV_FIELDS = {
    "LLM_API_URL": "llm_api_url", "LLM_API_KEY": "llm_api_key", "LLM_MODEL": "llm_model",
    "EASYSCHOLAR_SECRET_KEY": "easyscholar_key", "NCBI_API_KEY": "ncbi_api_key",
    "NCBI_EMAIL": "ncbi_email", "OPENALEX_API_KEY": "openalex_key", "GH_TOKEN": "github_token",
}


def load_settings(root, overrides=None):
    root = Path(root)
    values = {}
    path = root / "config.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        allowed = set(Settings.__dataclass_fields__) - set(ENV_FIELDS.values())
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError("config.json 含未知字段或密钥字段；密钥只放 .env / GitHub Secrets")
        values.update(data)
    # Only THIS project .env is read; no implicit unrelated repository config.
    env = {}
    path = root / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    env.update(os.environ)
    for key, field in ENV_FIELDS.items():
        if env.get(key):
            values[field] = env[key]
    values.update(overrides or {})
    settings = Settings(**values)
    bounds = {"max_new_per_day": (0, 5), "max_ai_calls": (0, 20), "max_requests": (1, 500),
              "discovery_limit": (1, 100), "repair_limit": (0, 20), "candidate_checks": (0, 20),
              "bootstrap_batch_limit": (1, 50),
              "max_images": (0, 5), "max_supplements": (0, 5), "max_file_bytes": (1024, 10000000)}
    for key, (low, high) in bounds.items():
        value = getattr(settings, key)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{key} 必须为 {low}—{high} 的整数")
    for key in ("include_supplements", "citation_enrichment", "initialize_existing_first"):
        if type(getattr(settings, key)) is not bool:
            raise ValueError(f"{key} 必须为布尔值")
    if not isinstance(settings.discovery_query, str) or not 1 <= len(settings.discovery_query.strip()) <= 20000:
        raise ValueError("discovery_query 必须是 1—20000 字的检索式")
    for key in ENV_FIELDS.values():
        value = getattr(settings, key)
        if not isinstance(value, str) or len(value) > 20000 or any(c in value for c in "\r\n\x00"):
            raise ValueError("API 配置字段类型或长度不正确；不显示密钥值")
    return settings
