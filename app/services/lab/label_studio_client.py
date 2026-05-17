"""
Label Studio HTTP API 极简客户端。

只暴露两个能力：
- ping(): GET /api/version，用于探活
- import_clip(): POST /api/projects/{project_id}/import (multipart)，上传一个 mp4 创建 task

实现选型：沿用 app/services/persistence/strategies/alarm_strategy.py 的 urllib.request
模式，避免引入 requests/httpx 依赖。multipart 手工拼装。

注意：当前实现把整个 mp4 读进内存。Lab 场景下 clip 通常 <5 min，可接受；
更大文件需重写为 streaming（urllib 对 BufferedReader 友好，但 boundary 需自己分块写）。
"""

from __future__ import annotations

import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LabelStudioTaskResult:
    """LS 单次 import 的结果。"""

    success: bool
    task_id: Optional[int]      # LS 分配的 task id（成功时）
    error: Optional[str]        # 失败时的简短描述
    error_code: Optional[str]   # 'ls_unreachable'|'ls_auth'|'ls_bad_response' | None


class LabelStudioError(Exception):
    """LS 客户端通用异常。"""


def _build_multipart(
    file_path: Path, file_field: str, meta: Optional[dict]
) -> tuple[bytes, str]:
    """构造 multipart/form-data body。

    Returns:
        (body_bytes, content_type)
    """
    boundary = "----CleanSightLab" + secrets.token_hex(8)
    parts: list[bytes] = []

    if meta:
        meta_json = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="data"\r\n'
                f"Content-Type: application/json\r\n\r\n"
            ).encode("utf-8")
        )
        parts.append(meta_json)
        parts.append(b"\r\n")

    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"\r\n'
            f"Content-Type: video/mp4\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class LabelStudioClient:
    """Label Studio API 的最小可用封装。"""

    def __init__(self, base_url: str, token: str, timeout: int = 60):
        """
        Args:
            base_url: LS 根地址，如 http://10.176.122.22:8080
            token: LS 个人 API token
            timeout: 单次请求超时（秒）

        Raises:
            ValueError: base_url 或 token 为空
        """
        if not base_url:
            raise ValueError("LabelStudioClient: base_url is required")
        if not token:
            raise ValueError("LabelStudioClient: token is required")
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = int(timeout)

    @property
    def base_url(self) -> str:
        return self._base

    def ping(self) -> tuple[bool, Optional[str]]:
        """GET {base}/api/version。

        Returns:
            (reachable_and_authorized, error_message_if_any)
        """
        url = f"{self._base}/api/version"
        req = Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "CleanSightBackend/lab",
            },
            method="GET",
        )
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                _ = resp.read(2048)
            return True, None
        except HTTPError as e:
            return False, f"HTTP {e.code}: {e.reason}"
        except URLError as e:
            return False, f"URL error: {e.reason}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def import_clip(
        self,
        project_id: int,
        mp4_path: Path,
        meta: Optional[dict] = None,
    ) -> LabelStudioTaskResult:
        """上传一个 mp4 文件到 LS，作为指定 project 的一条 task。

        Args:
            project_id: LS project id
            mp4_path: 本地 mp4 路径
            meta: 透传到 task data 的额外字段（JSON）；LS 会把它合并到 task.data
        """
        if not mp4_path.exists() or not mp4_path.is_file():
            return LabelStudioTaskResult(
                success=False,
                task_id=None,
                error=f"clip not found: {mp4_path}",
                error_code="ls_bad_response",
            )

        url = f"{self._base}/api/projects/{int(project_id)}/import"
        body, content_type = _build_multipart(mp4_path, "file", meta)
        req = Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": content_type,
                "User-Agent": "CleanSightBackend/lab",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001
                pass
            code = "ls_auth" if e.code in (401, 403) else "ls_bad_response"
            return LabelStudioTaskResult(
                success=False,
                task_id=None,
                error=f"HTTP {e.code}: {e.reason} | body={err_body}",
                error_code=code,
            )
        except URLError as e:
            return LabelStudioTaskResult(
                success=False,
                task_id=None,
                error=f"URL error: {e.reason}",
                error_code="ls_unreachable",
            )
        except Exception as e:  # noqa: BLE001
            return LabelStudioTaskResult(
                success=False,
                task_id=None,
                error=f"{type(e).__name__}: {e}",
                error_code="ls_unreachable",
            )

        # LS /import 返回 {"task_count": N, "task_ids": [...], ...} 或类似
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            return LabelStudioTaskResult(
                success=False,
                task_id=None,
                error=f"non-JSON response: {e}",
                error_code="ls_bad_response",
            )

        task_id = _extract_first_task_id(payload)
        if task_id is None:
            return LabelStudioTaskResult(
                success=False,
                task_id=None,
                error=f"response has no task_ids: {str(payload)[:300]}",
                error_code="ls_bad_response",
            )

        return LabelStudioTaskResult(
            success=True,
            task_id=task_id,
            error=None,
            error_code=None,
        )


def _extract_first_task_id(payload) -> Optional[int]:
    """从 LS /import 响应中提取首个 task_id。

    兼容 LS 不同版本的返回 schema：
      - {"task_ids": [1,2,3], ...}
      - {"task_count": 1, "annotation_count": 0, ...}（无 ids，需读 first key）
      - 直接列表 [{"id": 1}, ...]
    """
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and "id" in item:
                try:
                    return int(item["id"])
                except (TypeError, ValueError):
                    pass
        return None
    if isinstance(payload, dict):
        ids = payload.get("task_ids")
        if isinstance(ids, list) and ids:
            try:
                return int(ids[0])
            except (TypeError, ValueError):
                return None
        # LS 较新版本：返回 import job 对象，不直接给 task_id
        if "id" in payload:
            try:
                return int(payload["id"])
            except (TypeError, ValueError):
                return None
    return None
