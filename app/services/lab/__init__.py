"""
Lab 视频段导出与 Label Studio 提交。

模块：
- clip_builder: 按 [start_ms, end_ms] 区间从 raw 段拼接出单个 ms 精度 mp4（重编码）
- step_exporter: 把整个 (task_id, step_id, track) 导出为单个 mp4（纯 remux，不重编码）
- label_studio_client: Label Studio HTTP API 极简客户端（multipart 上传 mp4）
- runtime_config: 送标页面可改的 Label Studio 运行时状态（持久化到 JSON）

被路由层 app/routers/lab.py 使用。本 `__init__` 不做 re-export，调用方走深路径：

    from app.services.lab.clip_builder import ClipBuilder, ClipSpec
    from app.services.lab import runtime_config
"""
