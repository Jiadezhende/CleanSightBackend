"""本域的两个写侧动作：`insert_segment`（写一段）与 `delete`（清掉整个 step 的 hls 产物）。

`insert_segment` 一次调用，从内存帧序列到 `hls/` 目录里一个可播的段：调用方交出
`Sequence[Frame]`，拿回这段的身份键。中间那七步（编码、转码、位置修补、索引、init、
清单、统计）全在层内，**没有一步漏到签名上**。

## 事务形态：规范 §7.2 路线 A

    ① stage    在 {step}/hls/.stage_{track}_{ts_us}/ 里造产物
               ├ cv2 写 mp4v（_encode）
               └ ffmpeg 转 fMP4，得 fragment + init（_fmp4）
    ② adjust   读既有清单求累计 EXTINF → hex-patch fragment 的 tfdt（位置相关）
    ③ commit   按 W8 的顺序把 stage 里的东西搬进域目录并登记：
               sidecar → init → 段文件 → 清单条目 → 统计
    任一步异常 → 删 stage、不 rename、不登记，原异常上抛

**为什么必须分 stage**：段文件名一出现，读侧就认为它是合法产物（`SegmentFinder` 认的
就是这个名字）。原地编码会留下一个"文件在、但还是 mp4v 不是 fragment"的窗口（实测
~260 ms）。放进 stage 目录后，`.stage_` 开头既不匹配段正则、也不是文件，读侧天然看不见。

**为什么 commit 是那个顺序**（W8，三条写反都不报错）：

    sidecar 先于段文件      反过来会留下「段可见但索引未就位」的窗口，离线反查此时拿不到 ts
    init 先于清单头         清单头里的 EXT-X-MAP 指着 init，指向一个不存在的文件 = 播放器直接报错
    段文件先于清单条目      清单有行无文件 = 播放器直接报错

## 并发：**本域不持锁，串行由调用侧的队列构造**

前提是：**同一 `(task, step, track)` 的 `insert_segment` 串行调用**，且与该 step 的
`delete` / `purge_step` 同序——即提交到同一条 `app.utils.task_queue.SerialTaskQueue`。

破了这条前提会怎样：两段并发进来会读到同一个累计 EXTINF → tfdt 碰撞 → 后段在播放器里
覆盖前段。**不报错、不卡顿，只是画面丢一截。** 规范 §7.4 C1 原本要本层自己持 step 锁，
这一条已被队列方案推翻——顺序由提交序构造出来比抢锁更强（它顺带保证「旧残段先落盘、
再整个删掉」），但代价是这个不变式落在层外、门禁抓不到，所以写在这里。

不同 track、不同 step 之间互不冲突：各写各的段名与清单，唯一的共享产物 `metadata.json`
是派生量，两轨同时记账最多丢一次计数、不影响播放。

依赖上界：`app.domain`（域货币 `Frame`）+ stdlib。
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Sequence

from app.domain.frame import Frame

from . import _encode, _fmp4, _idx, _layout, _m3u8, _meta
from ._layout import SegmentRef

logger = logging.getLogger(__name__)


def insert_segment(
    task_id: int,
    step_id: int,
    track: str,
    frames: Sequence[Frame],
) -> SegmentRef:
    """把一段帧写成该 step 下 `track` 轨的一个 HLS 段，返回它的身份键。

    Args:
        task_id: 任务 id。
        step_id: 洗消步骤 id。
        track: `"raw"` 或 `"processed"`。**无默认值**——写错轨不会报错，只是回放时
            两条轨的画面串了（两轨各自独立、都合法）。
        frames: 该段的帧序列，按时间升序。段的起始时刻取首帧 ts。

    Returns:
        `SegmentRef(track, ts_us)` —— 段在本 step 内的身份键，可用它经 `segment_path`
        等定位函数拿到任一产物的路径。

    Raises:
        ValueError: track 非法，或 `frames` 为空。空段不是"没事发生"而是调用方算错了
            批次——本层不替它把这件事当成功（也不能：得返回一个不存在的段的身份）。
        OSError: 建目录 / 编码 / 落盘失败（cv2 的失败也翻译成这一档，见 `_encode`）。
        subprocess.CalledProcessError | subprocess.TimeoutExpired | FileNotFoundError:
            ffmpeg 非零退出 / 超时 / 机器上没有 ffmpeg。
        RuntimeError: ffmpeg 报成功却没产出，或 tfdt 改写失败（见下）。

    **失败要不要重试、重试几次，是调用方的策略**（§7.0）：本层只负责把"这次是环境坏了
    还是数据坏了"如实抛出来，并保证失败时域目录里不多一个产物。

    **tfdt 改写失败是致命的，不降级**：一个 tfdt 没修好的 fragment 进了清单，播放器会
    把它落在媒体轴原点、覆盖前面的段——不报错、不卡顿，只是画面丢一截。这类静默错正是
    本层存在的理由，所以宁可整段作废、让它在日志里喊出来。它只会在 fragment 结构与预期
    不符时发生（ffmpeg 换代），那是系统性问题，压着不说会静默烂掉整批录像。

    **sidecar 写失败不致命**：它只服务离线反查，回放/下载/送标三条链路都不读它；而读侧
    本就按契约容忍缺 sidecar（跳过该段、不打断迭代）。拿整段视频给一个辅助索引陪葬是
    坏交换，故降级为 warning。
    """
    _layout.require_track(track)
    if not frames:
        raise ValueError(f"frames 为空，无法生成段: task_id={task_id} step_id={step_id} track={track}")

    start_ts = frames[0].timestamp
    ref = SegmentRef(track=track, ts_us=_layout.ts_to_us(start_ts))

    # 编码帧率与媒体时长同源：VideoWriter 用它、EXTINF 用它、tfdt 由 EXTINF 累加而来。
    fps = _encode.effective_fps(frames)
    duration_s = _encode.media_duration(len(frames), fps)

    # create=True 只在这里做一次，顺带把 hls/ 建出来（域名白名单在 `_root` 那步校验）
    segment_target = _layout.segment_path(task_id, step_id, ref, create=True)
    init_target = _layout.init_path(task_id, step_id, track)
    playlist = _layout.playlist_path(task_id, step_id, track)

    stage = _layout.stage_dir(task_id, step_id, ref)
    # 入口清一次即幂等：同键重试会复用同一个目录名，不清则上次的半成品还在里面
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()

    try:
        # ① stage —— 锁外的重活：编码 + 转码，产物全在 stage 里，读侧看不见
        _encode.write_mp4v(_fmp4.source_path(stage), frames, fps)
        fragment, init = _fmp4.transcode(stage)

        # ② adjust —— 位置相关的最后修补：本段在媒体轴上的落点 = 此前所有 EXTINF 之和。
        # 必须在把本段条目追加进清单**之前**读，此刻清单恰好只含 0..N-1 段。
        offset_s = _m3u8.total_duration(playlist)
        if offset_s > 0.0:  # 首段落点本就是 0，无需改写
            if not _fmp4.patch_tfdt(fragment, _fmp4.seconds_to_ticks(offset_s)):
                raise RuntimeError(
                    f"tfdt 改写失败，整段作废（fragment 结构与预期不符）: {segment_target.name}"
                )

        # ③ commit —— 顺序即 W8，三条写反都不报错
        if track == "raw":
            # 只有 raw 轨产出 sidecar：processed 是渲染结果、离线不消费
            try:
                _idx.write(
                    _layout.sidecar_path(task_id, step_id, ref),
                    [frame.timestamp for frame in frames],
                )
            except OSError as e:
                logger.warning(
                    "[storage.hls] sidecar 写入失败，该段离线不可反查（视频照常落盘）: %s", e
                )

        if not init_target.exists():
            # 每 track 首次写入落盘；已存在则丢弃 stage 里那份 —— 同 track 同摄像头、
            # 同编码参数，SPS/PPS 一致，换成新的没有收益，却会让已发布的清单换 init
            os.replace(init, init_target)

        os.replace(fragment, segment_target)
        _m3u8.append(playlist, _layout.init_name(track), duration_s, segment_target.name)
        _meta.record_segment(
            _layout.metadata_path(task_id, step_id),
            task_id=task_id,
            step_id=step_id,
            track=track,
            duration_s=duration_s,
            timestamp=start_ts,
        )
    finally:
        # 成功路径留下的是 source.mp4 与 ffmpeg 自己那份 index.m3u8；失败路径留下半成品。
        # 两种都该走，故放 finally 而不是 except。
        shutil.rmtree(stage, ignore_errors=True)

    logger.info(
        "[storage.hls] 段已落盘: task_id=%s step_id=%s %s frames=%d duration=%.3fs fps=%.2f",
        task_id, step_id, _layout.segment_name(ref), len(frames), duration_s, fps,
    )
    return ref


def delete(task_id: int, step_id: int) -> bool:
    """删掉本域在该 step 下的**全部**产物（整个 `{step}/hls/` 目录）。

    Returns:
        该目录此前是否存在。删除失败记 warning 后返回 False——它的调用场景是「新一代
        开写前清掉上一代」，报不报错都得继续往下写，抛出去只会把一次录制整个葬掉。

    **只执行，不判断该不该删**：「这是不是新一代的首次写入」是 run 生命周期语义，归
    `recording`（与 `feature.remove_features` / `tasks.purge_step` 同款分工）。

    **只删本域**：同 step 的 `features/` 与 `lab/` 一个字节都不碰——这正是产物按域隔离
    换来的东西。域目录本身一起删掉，下次 `insert_segment` 的 `create=True` 会重建它。

    **不加锁**：串行由调用侧的队列构造，与 `insert_segment` 同一前提（见上方「并发」）。
    破了那条前提，这里的 `rmtree` 会与在途的段写撞车，表现是目录删到一半或写侧把目录
    重建出来、留一个已被记账删除的僵尸 step——两种都不报错。
    """
    domain_dir = _layout.domain_dir(task_id, step_id)
    if not domain_dir.exists():
        return False
    try:
        shutil.rmtree(domain_dir)
        return True
    except OSError as e:
        logger.warning("[storage.hls] 删除域目录失败 %s: %s", domain_dir, e)
        return False
