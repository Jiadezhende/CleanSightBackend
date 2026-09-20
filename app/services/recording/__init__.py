"""recording 模块 —— HLS 录制落盘的编排者。

把 CQ 里攒好的帧变成盘上一个可播的 HLS 段：**什么时候拉、按什么顺序写、算哪一代的产物**。
格式怎么落盘不在这里，那全在数据层 `app.storage.hls`。

    对外          app/services/recording/
      __init__.py   本文件：lifespan()
      instance.py   recording_service 单例
      service.py    RecordingService —— start / stop / collect_from /
                                        submit_segment / flush_residual /
                                        request_residual_flush / forget_task
      config.py     RecordingConfig（config/recording_config.yaml）
    包内私有
      _sweeper.py   节拍器：周期触发 service.collect_from，只由 RecordingService 构造

本 `__init__` 不做 re-export（规范 §3 的「门面型」：只有 docstring + `lifespan()`）——顶层
re-export `service` 会把 `app.storage.hls` 与 client → numpy 那条链摊给每个 import 本包的
人。消费方走深路径：

    单例   from app.services.recording.instance import recording_service
    类     from app.services.recording.service import RecordingService
    配置   from app.services.recording.config import RecordingConfig, get_recording_config

## 已接线（生产写侧就是这里）

`app/main.py` 嵌 `lifespan()`（persistence 同一档、inference 外层），`run_control` 拆除时调
`flush_residual` / `forget_task`，产物落 `{task}/{step}/hls/`。

`persistence/strategies/hls_strategy.py` 那一套旧写侧**代码仍在但已不启动**：
`PersistenceManager.start()` 不再起 `hls_pool` 与 `HLSSegmentSweeper`。⚠ **别把它们起回来**
——两个 sweeper 都从活跃 CQ 破坏性 drain，同时跑会各拿走一半帧、产出两份互相缺帧却都自洽的
段，两端都不报错。
"""

from contextlib import asynccontextmanager

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan():
    """recording 服务生命周期（**起于 inference 之前、停于 inference 之后**）。

    在 `app/main.py` 里嵌在 `inference.lifespan` 外层，与 persistence 同一档，理由也一样：
    `inference.stop()` 会经 `run_control` 交出最后一批 HLS 残段，那时队列必须还活着；
    等它交完，本 `finally` 再停队列、把剩下的排空——**保序、不丢尾**。

    嵌到 inference 里层会让队列先停，残段提交被拒，而那些帧已经从 CQ 弹出去了，是真丢。

    单例 import 写在函数体内（规范 §3）：写在模块级就等于把上面那笔过路费又收回来。
    """
    from app.services.recording.instance import recording_service

    recording_service.start()
    try:
        yield
    finally:
        recording_service.stop(timeout=10.0)
