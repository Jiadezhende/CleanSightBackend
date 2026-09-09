"""
storage —— `{storage_root}/` 下落盘布局的唯一真源。

**零跨服务依赖的 leaf**：本包不 import 任何其他 `app.services.*`，故写侧 persistence 与
读侧 traceback / lab / inference.offline / routers 都可以向下依赖它（门禁见
`tests/test_import_hygiene.py`）。

## 落盘结构：`(task_id, step_id)` 是身份键，域子目录是隔离边界

    {root}/{task_id}/{step_id}/
      hls/       {track}_segment_{ts_us}.mp4 / {track}_init.mp4
                 {track}_playlist.m3u8 / raw_segment_{ts_us}.idx / metadata.json
      features/  features.jsonl / facts.jsonl
      lab/       送标 clip 与整段导出的临时件（用完即删，残留随 step TTL 回收）

**step 根下只有域目录、没有文件。** 域名过 `_root.DOMAINS` 白名单，笔误
（`"feature"` / `"HLS"`）当场 `ValueError` —— 否则它会静默造出第四个子目录：写侧不报错、
读侧只是"查不到"、`purge_step` 照样删掉，连残留证据都不留。各域文件把自己的域名绑在一个
一行私有 helper 里，域名在每个域文件中只出现一次。

**存储根下只有数字命名的 task 目录**，没有别的住户——`.lab_exports` 这类寄居者已随
lab 产物归入 `{task}/{step}/lab/` 而消失。

## 对外契约：传 `(task_id, step_id)` 和参数，拿到已定位的路径或已解析的事实

对外按**域**分文件，一域一模块，都是模块函数、不出句柄；每个入口都以 `(task_id,
step_id)` 这对身份键开头，本包不提供任何脱离身份键的能力：

    tasks.py     把 step 目录当整体看：有哪些 task / 有哪些 step / 整个删掉
    hls.py       段 / init / playlist / sidecar / metadata   （尚未落地）
    feature.py   features.jsonl / facts.jsonl                （尚未落地）
    lab.py       送标与导出的临时件                            （尚未落地）

    from app.services.storage import hls, tasks as step_tasks
    hls.segment_path(task_id, step_id, "raw", ts_us)   # 域内定位归域文件
    step_tasks.purge_step(task_id, step_id)            # 跨域操作归 tasks

**定位能力不出现在 `tasks.py`**：往某个域里写东西是那个域自己的事。每个域文件开头先声明
一个**绑死自己域名的私有 root**（`_domain_root(task_id, step_id, *, create=False)`），域内
所有路径函数都经它——域名在一个文件里只出现一次，写错一眼可见。样板与命名坑见
`_root.py` 的「各域文件的用法」。`tasks.py` 只在「跨所有域」时出面。

下划线开头的模块是**包内私有**，只供包内各域文件使用：

    _root.py     域名白名单 + 逐级定位（可选建目录）。不枚举、不删除

## 边界：本包只管「名字与定位」，不管「内容怎么读写」

判据是「搬进来会不会让本包吃 L2 依赖或持有状态」。**包名叫 `storage`，听起来什么存储
相关的事都能进，所以下面这份清单是本包唯一的边界声明**——往里塞东西前先看它：

    ffmpeg 转码参数 / tfdt hex-patch / timescale pin   persistence/hls_strategy 独家
    cv2.VideoWriter / eff_fps 反推                     同上（三者是一个原子正确性约束：
                                                       eff_fps 同时决定媒体时长、EXTINF、tfdt）
    段解码起 ffmpeg 子进程                             inference/offline
    FeatureStore 批缓冲 / owner fence / open_fresh     inference/feature（有状态）
    目录锁                                             persistence（并发机制不是格式）
    TTL 保留天数 / 扫描周期 / 删不删                    persistence/workers（策略）
    MediaToken / HTTP 状态码 / token 化 URL            routers（鉴权与表示层）
    异步调度（队列 / WorkerPool / strategy 分发）       persistence 的本职
    **Label Studio 运行时配置**                        `settings` 的路径字段 + lab/config.py
                                                       —— 它是配置不是产物，没有 (task, step)
                                                       身份键，本包一概不认识它

## 设计约束（新增成员前先过一遍）

1. **一域一模块**，函数带域限定词——`hls.*` 不可能被要求回答 `features.jsonl` 在哪，
   名字本身就在挡膨胀。
2. **准入判据**：这个知识有几个包会因为它变了而出错？< 2 → 不进（留在那个唯一的主人那里）。
3. **路径出包，根不出包**。不出路径就得把每个用到路径的动作（ffmpeg `cwd`、临时 m3u8、
   `FileResponse`）都搬进来，那正是上帝类的长法。
4. **不出句柄，只出模块函数**。句柄可携带，"顺手加个成员"总有地方加。
5. **能用参数解决的不拆方法**；但**涉及正确性的参数不给默认值**——漏传是 `TypeError`，
   不是静默走错分支。
6. **朴素**：只回答格式事实，不做业务判断、不定义错误语义（不出领域异常）。
7. **能力太窄的不包装成接口**：调用方自己一行就能写对的动作，包装它只是多一层。

本包是**标记型** `__init__.py`（无活体，规范 §3）：纯 docstring，不 re-export，
消费方走深路径 `from app.services.storage import tasks`。
"""
