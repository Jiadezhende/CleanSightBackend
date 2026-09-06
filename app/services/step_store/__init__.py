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
    playlist.py         m3u8 读（EXTINF）与 VOD 骨架写
    segment_decoder.py  段 + init → 像素帧。**会起 ffmpeg 子进程**，是本包唯一有
                        进程管理的模块（看门狗 + 临时文件 stderr）

sidecar（`.idx`）的命名在 layout、内容解释在 segment_decoder —— 同一个文件的名字与
内容必须同居本包，否则「段内帧号 n ↔ sidecar 下标 k 严格 1:1」这个地基的两端会隔着
包互相看不见。它破了不报错，只静默取错帧。

**不属于本包**（划清边界，防后人往里塞）：

    异步调度（队列 / WorkerPool / strategy 分发）  persistence 的本职
    TTL 的调度与策略：保留天数、扫描周期、删不删   同上。本包只回答"这目录里有什么、
                                                  最后何时活动"，不决定"该不该删"
    ffmpeg 转码参数 / tfdt patch / timescale pin  persistence/hls_strategy 写侧独家，零重复
    FeatureStore 的批缓冲 / owner fence           有状态机制，属 inference/feature
    MediaToken                                    鉴权，不是格式，留 services/traceback
    各家的 ffmpeg cmd 构造与失败策略              五处完全不同，是真私有
    按 ts 对号取帧的严格匹配策略                  消费侧策略，属 inference/offline
    HTTP 状态码与 token 化 URL                    表示层，留 routers

写侧（cv2.VideoWriter / fMP4 转码 / tfdt patch）也**不进本包**：它零重复，三者是一个
原子的正确性约束（eff_fps 同时决定媒体时长、EXTINF 与 tfdt），且它吃 cv2（L2）——
搬进来会让本包从 L0/L1 leaf 变成 L2 包，导入门禁立刻红。
"""
