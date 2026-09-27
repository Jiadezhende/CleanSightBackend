"""
storage —— 数据层：内存数据模型与 `{storage_root}/` 下盘上数据之间的抽象。

调用方交出内存对象、拿回内存对象或已定位的 `Path`；文件名、目录布局、文本格式、二进制
布局、编解码全在本层。**设计规范与准入判据见 `docs/kb/DESIGN_STORAGE_LAYER.md`**，
新增域文件 / 新增成员前先读它。

## 落盘结构：一次运行一个目录，`RunIdentity(task, step, run_id)` 是身份键，域子目录是隔离边界

    {root}/{task_id}/{step_id}/{run_id}/
      hls/        {track}_segment_{ts_us}.mp4 / {track}_init.mp4
                  {track}_playlist.m3u8 / raw_segment_{ts_us}.idx / metadata.json
      inference/  detections.jsonl / temporal.jsonl / label_probs.npz
      lab/        送标 clip 与整段导出的临时件（用完即删，残留随 step TTL 回收）

run 目录只由 `runs.allocate` 建，写者只建域这一级。存储根下除数字命名的 task 目录外只有
`.trash/`（`_fs.remove` 的回收区）与 `.lab_exports/`。域名过 `_root.DOMAINS` 白名单，笔误当场 `ValueError`。

## 对外：一域一个 import 名，模块函数，不出句柄

    tasks.py     跨域：有哪些 task / 有哪些 step
    runs.py      run 目录 `{step}/{run_id}/`：allocate（唯一建产物目录者）/ query
    inference/   detections.jsonl / temporal.jsonl / label_probs.npz：推理链路产物
    hls/         段 / init / playlist / sidecar / metadata：定位、编解码、读写
    lab.py       送标与导出的临时件                                （尚未落地）

域读写口都以 `run: RunIdentity` 开头（迁移期读口另收 `(task_id, step_id)`，经 `runs.query`
解析）。域内定位归域文件，`tasks.py` / `runs.py` 只在跨所有域时出面。

    from app.storage import hls, runs, tasks as step_tasks
    run = runs.query(task_id, step_id)                         # None → 404
    ref = hls.insert_segment(run, "raw", frames)               # 交内存对象，拿身份键
    hls.segment_path(run, ref)                                 # 域内定位归域文件
    step_tasks.list_step_ids(task_id)                          # 跨域操作归 tasks

薄域用单文件、重域用子包，对外看不出区别（子包 `__init__` 是 facade，re-export 会连带
加载实现模块，故那些模块的模块级必须保持 stdlib-only）。

下划线开头的模块包内私有：

    _root.py     域名白名单 + 逐级定位 + 域目录只建一级。不枚举、不删除
    _fs.py       盘上原语：整体替换 / 原子删除（经 `.trash/`）/ 建一级目录，全包只此一份

## 依赖与边界（两条硬约束，细则在规范里）

- **依赖白名单**：stdlib、三方，加 `app.domain` 与 `app.settings`（只在函数体内）。别的
  `app.*` 一律不行，包括 `app.database` / `app.models`。门禁
  `test_layer_package_imports_only_whitelisted_app_modules`。
- **只收转换，不收策略 / 编排 / 业务语义**：TTL 留多久、失败重试几次、谁来调、并发几个、
  HTTP 状态码，全在层外。反之编解码（含起 cv2 / ffmpeg）是本层本职。
- **本层不持锁**：同一 run 的写由调用侧提交到同一条 `SerialTaskQueue` 串行，各域写入口的
  docstring 写明这个前提。

本包是标记型 `__init__.py`：纯 docstring，不 re-export，消费方走深路径
`from app.storage import tasks`。
"""
