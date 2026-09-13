"""
storage —— 数据层：内存数据模型与 `{storage_root}/` 下盘上数据之间的抽象。

调用方交出内存对象、拿回内存对象或已定位的 `Path`；文件名、目录布局、文本格式、二进制
布局、编解码全在本层。**设计规范与准入判据见 `docs/kb/DESIGN_STORAGE_LAYER.md`**，
新增域文件 / 新增成员前先读它。

## 落盘结构：`(task_id, step_id)` 是身份键，域子目录是隔离边界

    {root}/{task_id}/{step_id}/
      hls/       {track}_segment_{ts_us}.mp4 / {track}_init.mp4
                 {track}_playlist.m3u8 / raw_segment_{ts_us}.idx / metadata.json
      features/  features.jsonl / facts.jsonl
      lab/       送标 clip 与整段导出的临时件（用完即删，残留随 step TTL 回收）

step 根下只有域目录、没有文件；存储根下只有数字命名的 task 目录。域名过 `_root.DOMAINS`
白名单，笔误当场 `ValueError`。

## 对外：一域一个 import 名，模块函数，不出句柄

    tasks.py     跨域：有哪些 task / 有哪些 step / 整个删掉
    feature.py   features.jsonl（facts.jsonl 未迁入，见该模块）
    hls/         段 / init / playlist / sidecar / metadata：定位、编解码、读写
    lab.py       送标与导出的临时件                                （尚未落地）

每个入口都以 `(task_id, step_id)` 开头——本层不提供脱离身份键的能力。域内定位归域文件，
`tasks.py` 只在跨所有域时出面。

    from app.storage import hls, tasks as step_tasks
    ref = hls.insert_segment(task_id, step_id, "raw", frames)  # 交内存对象，拿身份键
    hls.segment_path(task_id, step_id, ref)                    # 域内定位归域文件
    step_tasks.purge_step(task_id, step_id)                    # 跨域操作归 tasks

薄域用单文件、重域用子包，对外看不出区别（子包 `__init__` 是 facade，re-export 会连带
加载实现模块，故那些模块的模块级必须保持 stdlib-only）。

下划线开头的模块包内私有：

    _root.py     域名白名单 + 逐级定位（可选建目录）。不枚举、不删除

## 依赖与边界（两条硬约束，细则在规范里）

- **依赖白名单**：stdlib、三方，加 `app.domain` 与 `app.settings`（只在函数体内）。别的
  `app.*` 一律不行，包括 `app.database` / `app.models`。门禁
  `test_layer_package_imports_only_whitelisted_app_modules`。
- **只收转换，不收策略 / 编排 / 业务语义**：TTL 留多久、失败重试几次、谁来调、并发几个、
  HTTP 状态码，全在层外。反之编解码（含起 cv2 / ffmpeg）是本层本职。
- **本层不持锁**：同一 step 的写与 `purge_step` 由调用侧提交到同一条 `SerialTaskQueue`
  串行，各域写入口的 docstring 写明这个前提。

本包是标记型 `__init__.py`：纯 docstring，不 re-export，消费方走深路径
`from app.storage import tasks`。
"""
