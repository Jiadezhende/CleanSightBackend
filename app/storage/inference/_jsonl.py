"""本域的文本 IO 底座：JSONL 行框定 + 文本原子落盘。

    encode(records)        一批 record → 待写文本
    decode(path)           整个 JSONL → record 列表，文件不存在返 []
    write_atomic(path, s)  路线 C：同目录 tmp + os.replace

两份 JSONL 产物共用同一份行框定。`write_atomic` 同时服务 `temporal.jsonl` 与
`offline_debug.json`——两者都是「整批文本一次性替换」的同一个动作，与逐行格式无关。

**错误语义**：单行坏了跳过 + warning（R6），IO 失败 `OSError` 原样抛，包成什么由调用方定。

依赖上界：stdlib。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

logger = logging.getLogger(__name__)


def encode(records: Sequence[Mapping[str, Any]]) -> str:
    """一批 record → 待写文本。**整批先编码完再碰盘**，中途失败时盘上不留半行。"""
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)


def decode(path: Path) -> List[Dict[str, Any]]:
    """读整个 JSONL → record 列表。文件不存在返回 `[]`。

    `utf-8-sig` 容忍 Windows 手写文件的 UTF-8 BOM。坏行逐行隔离，不让一行毁掉整个文件。
    """
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError as e:  # json.JSONDecodeError 是它的子类
                logger.warning("[storage.inference] 跳过损坏行 %s: %s", path, e)
                continue
            # 合法 JSON 但不是对象（`123` / `[1,2]` 都能解析成功）同样算坏行：本域每行按契约
            # 是一条 record，放行会让 `.get` 在下游炸成 AttributeError。
            if not isinstance(rec, dict):
                logger.warning("[storage.inference] 跳过非对象行 %s: %r", path, rec)
                continue
            records.append(rec)
    return records


def write_atomic(path: Path, text: str) -> None:
    """整体替换一份文本产物：写同目录 tmp → `os.replace` 原子换名（路线 C）。

    tmp 与目标**同目录**（同卷才是原子换名，W1），点开头故匹配不上任何产物名。失败即整体
    作废：删 tmp、不换名、原异常上抛（W4），盘上保留替换前的旧文件。

    Raises:
        OSError: 建目录 / 写 tmp / 换名失败。是否吞掉由调用方定。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # 清 tmp 再失败不能盖掉原始错因
            pass
        raise
