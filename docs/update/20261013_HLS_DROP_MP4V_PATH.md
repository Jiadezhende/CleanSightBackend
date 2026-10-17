# 删除录像段 mp4v 中间编码旧路径

> **变更状态**：生效中（2026-10-13）
> **知识库**：已沉淀 → [DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md)、[ARCHITECTURE_PACKAGE_LAYERS.md](../kb/ARCHITECTURE_PACKAGE_LAYERS.md)（2026-10-17）

## 概述

上一批 [20261013_HLS_STDIN_SINGLE_ENCODE.md](20261013_HLS_STDIN_SINGLE_ENCODE.md) 已经把 `insert_segment` 切到 `_fmp4.encode`（帧经 stdin 单次编码），本批删除不再有调用方的旧路径：

- **[_encode.py](../../app/storage/hls/_encode.py)**：删除 `write_mp4v` 和 `_FOURCC`，只保留 `effective_fps` / `media_duration`。
- **[_fmp4.py](../../app/storage/hls/_fmp4.py)**：删除 `transcode`、`source_path`、`_SOURCE_NAME`。原先写在 `transcode` 上的两条 ffmpeg 硬约束（`cwd=stage` 只传 basename；用 `video_track_timescale` 锁定 timescale）移到 `encode` 的 docstring。
- **文档注释同步**：hls 包的 facade docstring、`_decode` 注释、导入门禁注释，都从「cv2 编码」改成「ffmpeg 编码」。

hls 域的写侧从此不再依赖 cv2。行为不变；全量 `pytest tests/` 1006 passed。

## 遗留风险 / 后续任务

| 待办 | 处理计划 |
|------|---------|
| KB 还引用旧符号：[ARCHITECTURE_PACKAGE_LAYERS.md](../kb/ARCHITECTURE_PACKAGE_LAYERS.md) §「函数体内 import」中的 `write_mp4v`；[DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md) T3 / D3 两处的 `write_mp4v` / `transcode`（D3 的超时现由 `encode` 内的 `threading.Timer` 看门狗实现） | 走 KB 融合流程时更新 |
