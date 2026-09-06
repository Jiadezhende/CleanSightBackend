"""
step_store —— `database/{task_id}/{step_id}/` 落盘格式的唯一真源。

**零跨服务依赖的 leaf**：本包不 import 任何其他 `app.services.*` 包，故谁都可以向下
依赖它（地位同 `client_manager`，见包结构规范 §6）。写侧 persistence 与读侧
traceback / lab / inference.offline / routers 都向它依赖，谁也不依赖谁 —— 抽出本包
之前，写侧不敢依赖读侧（`persistence → traceback` 方向别扭），只好把同一套格式知识
再写一遍，12 份重复副本就是这么来的。

模块（标记型 `__init__`，不 re-export，消费方走深路径）：

    layout.py           目录与文件命名。纯常量 + 纯函数，L0，任何人零成本 import
    finder.py           SegmentFinder / SegmentRef / StepRef，目录扫描 + 段级二分
    playlist.py         m3u8 读（EXTINF）

**不属于本包**（划清边界，防后人往里塞）：

    异步调度（队列 / WorkerPool / strategy 分发）  persistence 的本职
    TTL 的调度与策略：保留天数、扫描周期、删不删   同上。本包只回答"这目录里有什么、
                                                  最后何时活动"，不决定"该不该删"
    ffmpeg 转码参数 / tfdt patch / timescale pin  persistence/hls_strategy 写侧独家，零重复
    FeatureStore 的批缓冲 / owner fence           有状态机制，属 inference/feature
    MediaToken                                    鉴权，不是格式，留 services/traceback
    各家的 ffmpeg cmd 构造                        五处完全不同，是真私有
    HTTP 状态码与 token 化 URL                    表示层，留 routers
"""
