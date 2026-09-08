"""
step_store —— `{storage_root}/{task_id}/{step_id}/` 落盘格式的唯一真源。

**零跨服务依赖的 leaf**：本包不 import 任何其他 `app.services.*`，故写侧 persistence 与读侧
traceback / lab / inference.offline / routers 都可以向下依赖它（门禁见
tests/test_import_hygiene.py）。

## 对外契约：传 `(task_id, step_id)` 和参数，拿到想要的信息

对外只有两个模块，都是**模块函数，不出句柄**：

    store.py   `(task, step)` 目录契约 —— 所有域共享。目录里最后何时有人写过、怎么在里面开
               一个文件、整个目录怎么删、盘上有哪些 step。**不知道任何产物的名字**
    hls.py     HLS 视频落盘域 —— 段/init/playlist/sidecar 的读写。名字、m3u8 格式、在途段
               判据全在包内

    from app.services.step_store import store as step_store, hls
    hls.segments(task_id, step_id, "raw")
    hls.vod_playlist(task_id, step_id, "processed", encode_uri=...)
    step_store.open_file(task_id, step_id, "features.jsonl", "a")

**内部实现对外全部不可见**。下划线开头的三个模块是包内私有，门禁锁死（具名例外见
tests/test_import_hygiene.py）：

    _layout.py    目录投影 + HLS 五类命名 + `SegmentRef` + 活动标记名。纯常量与纯函数（L0）
    _playlist.py  m3u8 读（EXTINF）与 VOD 骨架写。**对外只出成品，不出骨架**
    _decoder.py   段 + init → 像素帧。本包唯一起子进程的模块，经 `hls.frames()` 用

**为什么不出句柄**：此前出过一个 `Step` 句柄，它在 4 个包 16 个签名里流通，沿途每个使用者提
一点要求，长成了 21 个成员的上帝类——跨包流通的货币住在实现包里，必然膨胀。改成模块函数后，
每个域的函数带着域的限定词，`hls.*` 不可能被要求回答 `features.jsonl` 在哪。

**路径不出包**：没有成员返回目录，也不出存储根 —— 目录 = 根 + 两级 id 的拼装公式，交出去等于
把布局复制一份到调用方，且门禁抓不到（`dir / "x"` 是普通 Path 拼接，不是 settings 访问）。要
ffmpeg 的 `cwd` 取 `store.scratch_path(...).parent` 或已定位文件的 `.parent`。

**谁的名字归谁**：只有**两个包必须达成一致**的名字才进本包（HLS 那五类：persistence 写、
traceback / lab / offline 读）。只有一个主人的名字留在主人那里 —— `features.jsonl` /
`facts.jsonl` / `offline_inference_result.json` 归 `inference/`，它们经
`store.file_path(task_id, step_id, name)` 落进同一个 step 目录，本包对其内容零知识。

sidecar（`.idx`）的命名在 `_layout`、内容解释在 `_decoder` —— 「段内帧号 n ↔ sidecar 下标 k
严格 1:1」这个地基破了不报错，只静默取错帧，故名字与内容必须同居本包。

**TTL 判据**：step 目录里一个空的活动标记文件的 mtime（`_layout.ACTIVITY_NAME`），由 `store`
的写入口顺带刷新——写者只要问包「往哪写」就已经记账，忘不了。本包只答「这目录最后何时活动」
并执行删除，**不决定该不该删**；策略在 `persistence/workers/cleanup_worker.py`。

**不属于本包**（划清边界，防后人往里塞）：

    异步调度（队列 / WorkerPool / strategy 分发）  persistence 的本职
    TTL 策略：保留天数、扫描周期、删不删       同上
    ffmpeg 转码参数 / tfdt patch / timescale   persistence/hls_strategy 写侧独家
    FeatureStore 的批缓冲 / owner fence        有状态机制，属 inference/feature
    MediaToken                                 鉴权不是格式，留 services/traceback
    按 ts 对号取帧的严格匹配策略               消费侧策略，属 inference/offline
    HTTP 状态码与 token 化 URL                 表示层，留 routers

写侧（cv2.VideoWriter / fMP4 转码 / tfdt patch）也不进本包：它零重复，三者是一个原子的正确性
约束（eff_fps 同时决定媒体时长、EXTINF 与 tfdt），且它吃 cv2（L2）—— 搬进来本包就不再是 leaf，
导入门禁立刻红。
"""
