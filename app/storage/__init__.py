"""
storage —— 数据层：内存数据模型与 `{storage_root}/` 下盘上数据之间的抽象。

调用方交出内存对象、拿回内存对象或已定位的 `Path`；文件名、目录布局、文本格式、二进制
布局、编解码，以及保证这些不被并发写坏，全在本层。上层不碰其中任何一样。

**位置：`app/storage/`，与 `app/services/` 平级、在它下面一层。** 它不是一个服务，是数据
层；放在 `app/services/` 里会让「它不许依赖任何服务」看起来像个需要解释的例外，放在这里
则是一条不需要解释的分层规则。

**依赖白名单**：本包只许 import stdlib、三方，以及 `app.domain`（内存数据契约）与
`app.settings`（落盘根的唯一来源，按 `_root.py` 的规矩只在函数体内）。别的 `app.*` 一律
不行——包括 `app.database` / `app.models`：它们进来不造环、不报错，只会把数据层绑死在 ORM
上。门禁 `test_layer_package_imports_only_whitelisted_app_modules`。

这条是写侧 persistence 与读侧 traceback / lab / inference.offline / routers 能同时向下
依赖它的**前提**——一旦它向上或向旁伸手，那一头就不能再依赖它。

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

对外按**域**划分，一域一个 import 名，都是模块函数、不出句柄；每个入口都以 `(task_id,
step_id)` 这对身份键开头，本包不提供任何脱离身份键的能力：

    tasks.py     把 step 目录当整体看：有哪些 task / 有哪些 step / 整个删掉
    feature.py   features.jsonl（facts.jsonl 暂未迁入，理由见该模块 docstring）
    hls/         段 / init / playlist / sidecar / metadata；`insert_segment` 收 Frame 出
                 SegmentRef，`read_segment` 是它的逆运算（收 SegmentRef 出 Frame，只服务
                 raw 轨）。编码解码与位置相关修补全在域内，段枚举亦然（VOD 清单构造尚未
                 迁入；并发串行按上层统一调度基建走，本域不自己加锁）
    lab.py       送标与导出的临时件                                  （尚未落地）

**薄域用单文件、重域用子包，对外看不出区别**：`from app.storage import hls, feature` 拿到
的都是一个域的公开面，调用方分不出 `hls` 是包还是模块。所以「薄域将来变重」是非破坏性
升级——`feature.py` 哪天要拆 `_jsonl.py` / `_facts.py`，改成 `feature/` 子包即可，零调用
点改动。子包的 `__init__` 是 **facade**（re-export 域的公开成员），与本包根的标记型定位
不同；代价是它会连带加载实现模块，故那些模块的模块级必须保持 stdlib-only。

    from app.storage import hls, tasks as step_tasks
    ref = hls.insert_segment(task_id, step_id, "raw", frames)  # 交内存对象，拿身份键
    hls.segment_path(task_id, step_id, ref)                    # 域内定位归域文件
    step_tasks.purge_step(task_id, step_id)                    # 跨域操作归 tasks

**定位能力不出现在 `tasks.py`**：往某个域里写东西是那个域自己的事。每个域文件开头先声明
一个**绑死自己域名的私有 root**（`_domain_root(task_id, step_id, *, create=False)`），域内
所有路径函数都经它——域名在一个文件里只出现一次，写错一眼可见。样板与命名坑见
`_root.py` 的「各域文件的用法」。`tasks.py` 只在「跨所有域」时出面。

下划线开头的模块是**包内私有**，只供包内各域文件使用：

    _root.py     域名白名单 + 逐级定位（可选建目录）。不枚举、不删除

## 边界：转换进层，策略 / 编排 / 业务语义不进

**包名叫 `storage`，听起来什么存储相关的事都能进，所以这份清单是本层唯一的边界声明**
——往里塞东西前先过这四问（规范 §7.0）：

    它是「内存模型 ↔ 盘上字节」的转换本身吗？          进层
    它是关于这个转换的策略（做不做、重试几次、留多久）？ 不进
    它是编排（谁调、何时调、排队、并行度）？            不进
    它是业务语义（HTTP 状态码、告警阈值、算不算停顿）？  不进

据此不进本层的东西：

    FeatureStore 批缓冲 / owner fence / open_fresh     inference/feature（有状态，D4）
    TTL 保留天数 / 扫描周期 / 删不删                    persistence/workers（策略）
    失败要不要重试、重试几次                            调用方（策略。层只负责答「这次是
                                                       环境坏了还是数据坏了」，R6）
    MediaToken / HTTP 状态码 / token 化 URL            routers（鉴权与表示层）
    异步调度（队列 / WorkerPool / strategy 分发）       persistence 的本职（编排）
    **Label Studio 运行时配置**                        `settings` 的路径字段 + lab/config.py
                                                       —— 它是配置不是产物，没有 (task, step)
                                                       身份键，本层一概不认识它

**曾在这份清单上、已随判据换代移入本层的四行**（别照旧版文档往外推）：ffmpeg 转码参数 /
tfdt hex-patch / timescale pin、cv2.VideoWriter / eff_fps 反推、目录锁、以及为编解码起
外部工具子进程。理由与推翻记录见规范 §7.0。

**已决（原 §7.8 未决项）**：段解码（mp4 → 像素帧）**进层**，`hls.read_segment` /
`hls.iter_frames` 已落地，与 `insert_segment` 互为逆运算——层是双向的，T1 的往返闭合到
段这一档。判据就是上面四问的第一条：把字节变回内存对象，是转换本身。留在层外的是
`FrameTracker.find` 的位级 ts 匹配（「ts 是帧的身份，配错帧比报错更坏」属业务语义，
第四条）。**调用点尚未迁移**，`inference/offline` 的 `Timeline` 仍是现役、读的还是旧的
平铺布局，两份并存到写侧切换那一刻为止。

## 设计约束（新增成员前先过一遍）

1. **一域一个 import 名**，函数带域限定词——`hls.*` 不可能被要求回答 `features.jsonl`
   在哪，名字本身就在挡膨胀。域内怎么拆文件是域自己的事，不影响对外。
2. **准入判据**：这个知识有几个包会因为它变了而出错？< 2 → 不进（留在那个唯一的主人那里）。
3. **路径出层，根不出层**。不出路径就得把每个用到路径的动作（`FileResponse`、送标导出）
   都搬进来，那正是上帝类的长法。
4. **不出句柄，只出模块函数**。句柄可携带，"顺手加个成员"总有地方加。层可以持有解析
   缓存与锁表，但那是层内状态，不交出去。
5. **能用参数解决的不拆方法**；但**涉及正确性的参数不给默认值**——漏传是 `TypeError`，
   不是静默走错分支。
6. **朴素**：只回答格式事实，不做业务判断、不定义错误语义（不出领域异常）。多态失败用
   事实枚举 + NamedTuple 表达，不是 `Optional[str]`。
7. **能力太窄的不包装成接口**：调用方自己一行就能写对的动作，包装它只是多一层。
8. **并发由调用侧的串行调度保证，本层不持锁**（推翻原「锁归层内」，见
   `app/utils/task_queue.py`）：同一 step 的写与 `purge_step` 提交到同一条
   `SerialTaskQueue`，顺序是**构造出来**的而不是抢出来的，跨域删除因此也不用单开机制。
   代价是这条保证不在层内、门禁抓不到，各域写入口的 docstring 必须写明它依赖这个前提。

本包是**标记型** `__init__.py`（无活体，规范 §3）：纯 docstring，不 re-export，
消费方走深路径 `from app.storage import tasks`。
"""
