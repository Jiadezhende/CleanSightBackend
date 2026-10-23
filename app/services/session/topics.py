"""跨模块共享的 topic 声明（decoder / dispatcher / recording / viz / health_monitor 之间的帧交接）。

    from app.services.session.topics import RAW
    sess.channel(RAW).push(frame)

只被一个模块用的 topic 由该模块自己声明（如 `inference/online/topics.py`）。
容量取自 `config/client_config.yaml` + settings；压力日志的 component / resource 名沿用 CQ 时代
（`client_queues` / `ca_*`），日志检索与看板不变。
"""

from app.services.utils.pressure import DEFAULT_HIGH_WATERMARK_RATIO

from .core import Fifo, Latest, PressureSpec, Topic

_COMPONENT = "client_queues"


def _cfg():
    from app.services.client.config import get_client_config

    return get_client_config()


def _segment_watermark(maxlen: int, segment_len: int) -> int:
    """整段被 sweeper 拉走的队列：深度在 0~1 段之间来回是常态，水位至少 1.5 段。"""
    return max(1, min(maxlen, max(int(maxlen * DEFAULT_HIGH_WATERMARK_RATIO), segment_len * 3 // 2)))


def _ready() -> Fifo:
    return Fifo(
        maxlen=_cfg().ca_ready_maxlen, on_full=Fifo.REJECT,
        pressure=PressureSpec(_COMPONENT, "ca_ready"),
    )


def _segment_fifo(resource: str, drop_reason: str) -> Fifo:
    cfg = _cfg()
    return Fifo(
        maxlen=cfg.ca_maxlen, on_full=Fifo.EVICT, drop_reason=drop_reason,
        pressure=PressureSpec(_COMPONENT, resource, _segment_watermark(cfg.ca_maxlen, cfg.ca_segment_len)),
    )


def _detections() -> Fifo:
    # 容量复用 ca_maxlen（600 条 FrameDetection ≈ 40s @15fps，远小于像素帧）；不报压力（同 CQ 时代）
    return Fifo(maxlen=_cfg().ca_maxlen, on_full=Fifo.EVICT, drop_reason="detection_backpressure")


# decoder → dispatcher：抽帧后待推理的 Frame。满拒新（推理跟不上就丢新帧）
READY = Topic("READY", _ready)
# decoder → recording：全帧率原始 Frame，sweeper 整段取走落 raw 轨。满挤旧
RAW = Topic("RAW", lambda: _segment_fifo("ca_raw", "raw_backpressure"))
# decoder → viz（叠框底图）、health_monitor（最后一帧 ts）：最新原始 Frame
LATEST_RAW = Topic("LATEST_RAW", Latest)
# 写回 → recording：帧级 FrameDetection 落盘缓冲，sweeper 每 tick 全部取走写 detections.jsonl。满挤旧。
# 与帧窗 DETECTION_WINDOW 是同一帧的两个去处，不能合并：帧窗按算子感受野裁剪，落盘只在满时丢
DETECTIONS = Topic("DETECTIONS", _detections)
# viz → recording：渲染后的 Frame，sweeper 整段取走落 processed 轨。满挤旧
PROCESSED = Topic("PROCESSED", lambda: _segment_fifo("ca_processed", "hls_backpressure"))
# viz → `WS /ai/video`：最新渲染 Frame
RENDERED = Topic("RENDERED", Latest)


def queue_depths(sess) -> dict:
    """admin / 状态汇总用的深度快照。键名沿用 CQ 时代（admin `queue_depths` 对外不变）。"""
    return {
        "ca_ready": len(sess.channel(READY)),
        "ca_raw": len(sess.channel(RAW)),
        "ca_processed": len(sess.channel(PROCESSED)),
        "ca_detections": len(sess.channel(DETECTIONS)),
        "has_rendered": sess.channel(RENDERED).get() is not None,
    }
