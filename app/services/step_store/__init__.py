"""
step_store —— `{storage_root}/{task_id}/{step_id}/` 落盘格式的唯一真源。

**零跨服务依赖的 leaf**：本包不 import 任何其他 `app.services.*`，故写侧 persistence 与
读侧 traceback / lab / inference.offline / routers 都可以向下依赖它（门禁见
tests/test_import_hygiene.py）。

## 对外契约：调用方只给 `(task_id, step_id)`，本包回答一切

    from app.services.step_store import store as step_store
    step = step_store.step(task_id, step_id)
    step.segments("raw") / step.vod_playlist("processed") / step.segment_path("raw", ts_us)

**函数一律模块限定，不裸导入**（`step` / `steps` / `tasks` 是调用方的高频局部变量名，
裸导入会被就地遮蔽）；类型与异常（`Step` / `SegmentRef` / `StepInitMissing`）按名导入。

**路径不出包**：没有成员返回目录，也不出存储根 —— 目录 = 根 + 两级 id 的拼装公式，交出去
等于把布局复制一份到调用方，且门禁抓不到（`dir / "x"` 是普通 Path 拼接，不是 settings
访问）。要 ffmpeg 的 `cwd` 取 `Step.scratch_path().parent`。

模块（标记型 `__init__`，不 re-export，消费方走深路径）：

    store.py            唯一对外面：`Step`（句柄）/ `SegmentRef`（值对象）/ 模块级函数
                        step / steps / tasks / purge_step / sweep_empty_tasks
    layout.py           HLS 5 类产物（segment / init / playlist / sidecar / metadata）的
                        目录与文件命名。纯常量 + 纯函数（L0），任何人零成本 import
    playlist.py         m3u8 读（EXTINF）与 VOD 骨架写。**包内私有**，两条具名例外见该
                        模块 docstring
    segment_decoder.py  段 + init → 像素帧。本包唯一起子进程的模块，经 `Step.frames()` 用

**写侧：每类产物一个具名成员**（`Step.segment_path(track, ts_us)` / `open_features(mode)`
/ …），签名即命名参数。新增落盘产物在 `Step` 上加一个成员即可，**不需要注册表** ——
「这目录还活着吗」由写入口顺带刷新的活动标记（`layout.ACTIVITY_NAME`）回答，与产物有哪些
无关，故新增产物不存在「忘登记就对 TTL 不可见」这回事。

sidecar（`.idx`）的命名在 layout、内容解释在 segment_decoder —— 「段内帧号 n ↔ sidecar
下标 k 严格 1:1」这个地基破了不报错，只静默取错帧，故名字与内容必须同居本包。

**不属于本包**（划清边界，防后人往里塞）：

    异步调度（队列 / WorkerPool / strategy 分发）  persistence 的本职
    TTL 策略：保留天数、扫描周期、删不删       同上。本包只答「这目录里有什么、最后何时
                                              活动」并执行删除，不决定「该不该删」；策略在
                                              persistence/workers/cleanup_worker.py
    ffmpeg 转码参数 / tfdt patch / timescale   persistence/hls_strategy 写侧独家
    FeatureStore 的批缓冲 / owner fence        有状态机制，属 inference/feature
    MediaToken                                 鉴权不是格式，留 services/traceback
    按 ts 对号取帧的严格匹配策略               消费侧策略，属 inference/offline
    HTTP 状态码与 token 化 URL                 表示层，留 routers

写侧（cv2.VideoWriter / fMP4 转码 / tfdt patch）也不进本包：它零重复，三者是一个原子的
正确性约束（eff_fps 同时决定媒体时长、EXTINF 与 tfdt），且它吃 cv2（L2）—— 搬进来本包就
不再是 leaf，导入门禁立刻红。
"""
