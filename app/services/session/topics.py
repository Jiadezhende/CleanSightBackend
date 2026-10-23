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


def _raw() -> Fifo:
    cfg = _cfg()
    return Fifo(
        maxlen=cfg.ca_maxlen, on_full=Fifo.EVICT, drop_reason="raw_backpressure",
        pressure=PressureSpec(_COMPONENT, "ca_raw", _segment_watermark(cfg.ca_maxlen, cfg.ca_segment_len)),
    )


# decoder → dispatcher：抽帧后待推理的 Frame。满拒新（推理跟不上就丢新帧）
READY = Topic("READY", _ready)
# decoder → recording：全帧率原始 Frame，sweeper 整段取走落 raw 轨。满挤旧
RAW = Topic("RAW", _raw)
# decoder → viz（叠框底图）、health_monitor（最后一帧 ts）：最新原始 Frame
LATEST_RAW = Topic("LATEST_RAW", Latest)
