# 旧落盘结构清理：删除迁移脚本，不再兼容历史产物

> **变更状态**：生效中（2026-09-02）
> **知识库**：已沉淀 → [kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md) / [kb/SERVICE_PERSISTENCE.md](../kb/SERVICE_PERSISTENCE.md) / [kb/SERVICE_TRACEBACK_MEDIA.md](../kb/SERVICE_TRACEBACK_MEDIA.md) / [kb/SERVICE_LAB.md](../kb/SERVICE_LAB.md) / [kb/ARCHITECTURE_API_SURFACE.md](../kb/ARCHITECTURE_API_SURFACE.md) / [kb/DESIGN_HLS_TIMELINE.md](../kb/DESIGN_HLS_TIMELINE.md)(2026-09-02)

## 决策

**旧落盘结构一律不做兼容，也不提供迁移路径。** 只保证新写入的产物可用；历史数据要恢复
可用性走回灌重跑，不做原地转换。

据此删除两个迁移脚本：

| 删除 | 原职责 |
|------|--------|
| `scripts/transcode_segments_to_h264.py` | 历史段就地升级为 fMP4 + 补 `{track}_init.mp4` + 重写 playlist 头 |
| `scripts/migrate_legacy_runs.py` | 旧目录结构 `{base_dir}/{client_id}/{task_id}/` → `{base_dir}/{task_id}/{step_id}/` |

两者均无代码 import，仅被错误措辞里的字符串引用。

## 当前落盘格式（写侧真源 = `hls_strategy`）

```text
{storage_base_dir}/{task_id}/{step_id}/
  raw_init.mp4                    # fMP4 init，按轨各一份
  processed_init.mp4
  {track}_segment_{ts_us}.mp4     # fMP4 fragment，mdhd.timescale pin 90000
  raw_segment_{ts_us}.idx         # raw 逐帧 ts sidecar（float64），仅供离线帧反查
  {track}_playlist.m3u8           # LIVE 形态，不写 ENDLIST
  metadata.json
  features.jsonl / facts.jsonl    # inference 侧，非 HLS 产物
```

已废弃、当前无任何读者、磁盘上可能仍有残留的旧产物：

- `init.mp4`（双轨共用，c83ee7d 前）
- `.hls_timescale`（timescale 缓存文件，cb9d473 pin 常量后删除）
- `.timeline.idx` + `.timeline.log`（单体时间索引，380041c，dc5a4ba 废弃）
- `raw_segment_{ts}.idx.json`（第二代 JSON sidecar，15c0237，dc5a4ba 废弃）

sidecar 三个月内换过三代格式：`.idx.json`(08-15) → `.timeline.idx`(08-21) → `.idx`(08-30)。

## 缺 init 时的行为（回放 / 下载）

`{track}_init.mp4` 缺失时，`/traceback/*/playlist.m3u8` 与 `/lab-f3m8/download` 均返回
**503**。措辞不再指向迁移脚本，改为说明两种可能且**服务端均无法自愈**：

1. 段是分轨命名之前的旧格式产物 —— 不支持；
2. 首段仍在 transcode 途中（窗口极短）。

前端按「此 step 不可回放 / 不可导出」处理，不必重试。

## sidecar 写失败不再拖垮视频段

`_update_timeline` 的 `OSError` 从 `raise PersistenceError` 降为 `logger.warning`。

改前：sidecar 排在 mp4 之前，抛出去会让 HLSWorker 的 GuardedExecutor 重试耗尽后**丢掉整段
视频**（mp4 / playlist / metadata 全不落）。而 `.idx` 只服务离线反查，回放/下载/送标三条
链路都不读它；读侧（`frame_tracker._load_sidecar`）本就按契约容忍缺 sidecar——跳过该段、
不打断整条迭代。让辅助索引拿主产物陪葬是反的。

实测（人为让某段 sidecar 写失败）：该段 mp4 正常落盘、进 playlist、计入 metadata；回放
拿到全部段，下载帧数不缺，送标可在该段上裁剪；离线迭代跳过该段并告警，故障不粘连相邻段。

## 同步改动

- `hls_strategy` 头部 docstring 补齐实际产物清单（原文写着早已不存在的「Keypoints JSON 序列化」）
- `tests/test_traceback_router.py::test_playlist_503_when_init_missing` 断言从整句措辞
  改为只认「指认了缺失的具体文件名」
- KB：`ARCHITECTURE_STORAGE_AND_SCHEMA` / `SERVICE_PERSISTENCE` / `SERVICE_TRACEBACK_MEDIA` /
  `SERVICE_LAB` / `ARCHITECTURE_API_SURFACE` / `DESIGN_HLS_TIMELINE`
- API 文档：`media.md` / `traceback.md` / `lab.md` / `guide-video.md` / `README.md`

## 已知未收口

- **送标链路不过滤在途段**（既有洞，非本次引入）：`ClipBuilder` 不读 playlist EXTINF，
  实测吃进未转码的裸 mp4v 段后**不报错**，请求 4s 窗口产出 2.077s / 27 帧的残片。回放和
  下载都有这道闸，只有送标没有。产物直接进 Label Studio，静默产错标注素材。
- **段级定位有三套实现**：`SegmentFinder.find`（bisect）、`Timeline.iter`（searchsorted）、
  `ClipBuilder._select_segments`（区间重叠 + 中位差估段尾）。根因是 `SegmentFinder` 缺区间
  版 API。落盘布局知识（init / segment / sidecar 命名）散在 7 个文件、段名正则曾有 3 份
  拷贝（删脚本后剩 2 份）。收敛方案倾向抽 `app/domain/storage_layout.py`（纯命名函数，写侧
  读侧共同向下依赖），待 `frame_tracker` 真正接入离线链路、区间查询形状定了再做。
