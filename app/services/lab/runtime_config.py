"""Lab / Label Studio 运行时配置。

`label_studio_url` 与 `default_project_id` 可在送标页面修改并持久化到 JSON 文件，
改完即时生效、重启后保留，无需重启后端。

token 不在此管理：恒等于 `settings.label_studio_token`（env），页面不可见、不可改，
密钥不经页面流转。

持久化文件：{storage.base_dir}/lab_runtime_config.json（database/ 已 gitignore）。
解析优先级：文件存在 → 用文件值；否则回退到 settings(env) 默认值。

并发：submit 在 threadpool 里多线程跑，读写都过 _LOCK。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_loaded = False
_url: str = ""
_default_project_id: int = 0
_source: str = "env"  # "file" | "env"

_CONFIG_FILENAME = "lab_runtime_config.json"


def _config_path() -> Path:
    """持久化文件绝对路径，与 HLS 段写入同一 base_dir。"""
    from app.services.traceback.segment_finder import get_default_base_dir

    return get_default_base_dir() / _CONFIG_FILENAME


def _ensure_loaded() -> None:
    """首次访问时惰性加载：文件优先，否则回退 env。调用方需已持有 _LOCK。"""
    global _loaded, _url, _default_project_id, _source
    if _loaded:
        return

    from app.settings import settings

    path = _config_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _url = str(data.get("label_studio_url", "") or "")
            _default_project_id = int(data.get("default_project_id", 0) or 0)
            _source = "file"
        except (OSError, ValueError, TypeError) as e:
            logger.warning(
                "[Lab] 读取 %s 失败，回退 env 默认值: %s", path, e
            )
            _url = settings.label_studio_url
            _default_project_id = settings.label_studio_default_project_id
            _source = "env"
    else:
        _url = settings.label_studio_url
        _default_project_id = settings.label_studio_default_project_id
        _source = "env"

    _loaded = True


def get_url() -> str:
    """生效的 LS base URL（文件值优先，回退 env）。"""
    with _LOCK:
        _ensure_loaded()
        return _url


def get_default_project_id() -> int:
    """生效的默认 project_id（文件值优先，回退 env）。"""
    with _LOCK:
        _ensure_loaded()
        return _default_project_id


def get_token() -> str:
    """LS token —— 恒来自 env，不可经页面修改。"""
    from app.settings import settings

    return settings.label_studio_token


def snapshot() -> dict:
    """供 GET /config 用的配置快照，不含 token 明文。"""
    with _LOCK:
        _ensure_loaded()
        return {
            "label_studio_url": _url,
            "default_project_id": _default_project_id,
            "token_configured": bool(get_token()),
            "source": _source,
        }


def update(label_studio_url: str, default_project_id: int) -> dict:
    """校验并持久化 url + default_project_id，更新内存。

    Args:
        label_studio_url: 为空表示「未配置」；非空必须以 http:// 或 https:// 开头
        default_project_id: >= 0；0 表示无默认值

    Returns:
        更新后的 snapshot()

    Raises:
        ValueError: 校验失败
    """
    url = (label_studio_url or "").strip().rstrip("/")
    if url and not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("label_studio_url 必须以 http:// 或 https:// 开头")
    pid = int(default_project_id)
    if pid < 0:
        raise ValueError("default_project_id 不能为负")

    global _loaded, _url, _default_project_id, _source
    with _LOCK:
        path = _config_path()
        payload = {"label_studio_url": url, "default_project_id": pid}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

        _url = url
        _default_project_id = pid
        _source = "file"
        _loaded = True
        logger.info(
            "[Lab] LS 运行时配置已更新: url=%s default_project_id=%d", url, pid
        )
        return {
            "label_studio_url": _url,
            "default_project_id": _default_project_id,
            "token_configured": bool(get_token()),
            "source": _source,
        }
