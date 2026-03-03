"""
CleanSight Prometheus Metrics（可观测性）

基于《实时 AI 视觉检测项目异常处理规范》：
- 5 个核心 metrics（最小集合）
- 强制埋点：推理延迟、失败率、丢帧率、GPU OOM、重试计数
- 用于 Grafana 可视化和告警
"""

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

# 使用默认 registry（与 FastAPI 集成）
# 如果需要自定义 registry，可以传入 registry=custom_registry

# ============================================================================
# 1. 推理延迟（Histogram）
# ============================================================================

infer_latency_ms = Histogram(
    "infer_latency_ms",
    "Inference latency in milliseconds",
    ["model"],
    buckets=[10, 25, 50, 100, 250, 500, 1000, 2500, 5000],
)
"""
推理延迟直方图

标签：
- model: 模型名称（如 'yolov8n'）

用途：
- 监控推理性能
- P50/P95/P99 延迟
- 告警：P99 > 1000ms

示例：
    start = time.time()
    results = model.infer(frames)
    elapsed_ms = (time.time() - start) * 1000
    infer_latency_ms.labels(model='yolov8n').observe(elapsed_ms)
"""


# ============================================================================
# 2. 推理失败计数（Counter）
# ============================================================================

infer_failure_total = Counter(
    "infer_failure_total",
    "Total inference failures",
    ["model", "error_type"],
)
"""
推理失败总数

标签：
- model: 模型名称
- error_type: 异常类型（如 'ModelInferenceError', 'FrameDrop'）

用途：
- 监控推理失败率
- 按异常类型分组
- 告警：失败率 > 5%

示例：
    try:
        results = model.infer(frames)
    except ModelInferenceError as e:
        infer_failure_total.labels(
            model='yolov8n',
            error_type='ModelInferenceError'
        ).inc()
        raise
"""


# ============================================================================
# 3. 帧丢弃计数（Counter）
# ============================================================================

frame_drop_total = Counter(
    "frame_drop_total", "Total frames dropped", ["reason"]
)
"""
帧丢弃总数（FrameDrop 专用）

标签：
- reason: 丢弃原因（如 'decode_failed', 'client_removed', 'quality_check_failed'）

用途：
- 监控单帧失败率
- 按丢弃原因分组
- 告警：丢帧率 > 10%

示例：
    if frame is None:
        frame_drop_total.labels(reason='decode_failed').inc()
        raise FrameDrop(client_id='client_1', reason='decode_failed')
"""


# ============================================================================
# 4. GPU OOM 计数（Counter）
# ============================================================================

gpu_oom_total = Counter("gpu_oom_total", "Total GPU out-of-memory errors", ["model"])
"""
GPU 内存不足错误总数

标签：
- model: 模型名称

用途：
- 监控 CUDA OOM 频率
- 触发批量大小调整
- 告警：OOM > 5 次/小时

示例：
    try:
        results = model.infer(frames)
    except RuntimeError as e:
        if "out of memory" in str(e).lower():
            gpu_oom_total.labels(model='yolov8n').inc()
            raise ModelInferenceError(message=str(e), is_cuda_error=True)
"""


# ============================================================================
# 5. 重试计数（Counter）
# ============================================================================

retry_total = Counter(
    "retry_total", "Total retry attempts", ["operation", "error_type"]
)
"""
重试总数（所有操作）

标签：
- operation: 操作类型（如 'stream', 'database', 'inference'）
- error_type: 异常类型

用途：
- 监控重试频率
- 按操作类型分组
- 告警：重试率 > 20%

示例：
    try:
        connect_stream(url)
    except StreamConnectionError as e:
        retry_total.labels(
            operation='stream',
            error_type='StreamConnectionError'
        ).inc()
        # GuardedExecutor 自动重试
"""


# ============================================================================
# 工具函数
# ============================================================================


def get_metrics() -> bytes:
    """
    获取所有 metrics（Prometheus 格式）

    Returns:
        bytes: Prometheus 文本格式的 metrics

    用途：
        在 FastAPI 中暴露 /metrics 端点

    示例：
        from fastapi import Response
        from app.utils.metrics import get_metrics

        @app.get("/metrics")
        def metrics():
            return Response(content=get_metrics(), media_type="text/plain")
    """
    return generate_latest()


def reset_all_metrics():
    """
    重置所有 metrics（仅用于测试）

    注意：生产环境不应调用此函数
    """
    # Counter 无法重置，只能通过重启进程
    # 此函数仅作为占位符，提醒不要在生产环境使用
    pass
