> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Traceback And Media Service

追溯服务负责按告警或任务步骤定位 HLS 段，并通过 token 化媒体路由返回可播放资源。

## SegmentFinder

`SegmentFinder` 基于目录约定：

```text
{base_dir}/{task_id}/{step_id}/
```

它按文件名中的 `ts_us` 列出和定位：

- raw segment
- processed segment

（keypoints JSON 死写已删，不再定位。）`find()` 根据目标 `ts_ms` 返回触发段和前后上下文。

## MediaToken

媒体 URL 不暴露物理路径。`MediaToken` 生成 HMAC-SHA256 短 TTL token，payload 包含：

- `task_id`
- `step_id`
- `filename`
- `kind`
- `expiry`

`settings.media_token_secret` 为空时，默认 token secret 在进程内生成，重启后旧 token 失效。

## /traceback/*

主要能力：

- 告警 evidence：返回 raw/processed clips、可选 detection JSON（keypoints URL 已下线）。
- task playlist：为单个 task + step + track 生成 VOD m3u8。
- alarm playlist：为告警上下文生成 VOD m3u8。
- timeline：返回该 step 的视频起止和告警事件。

## /media/*

媒体路由只接受 token：

- `/media/segment/{token}`：返回 mp4 fragment。
- `/media/init/{token}`：返回 `init.mp4`。

路径解析会拒绝 path traversal，并确保文件在 base_dir 内。

## VOD playlist 原则

VOD playlist 不直接暴露落盘 LIVE playlist，而是动态生成：

- 带 `#EXT-X-PLAYLIST-TYPE:VOD`
- 带 `#EXT-X-ENDLIST`
- 每个 segment URL 都是 token URL
- 只使用写入侧 playlist 中已有 EXTINF 的段，过滤在途段

## 代码来源

- `app/routers/traceback.py`
- `app/routers/media.py`
- `app/services/traceback/segment_finder.py`
- `app/services/traceback/media_token.py`
- `tests/test_traceback_router.py`
- `tests/test_traceback_segment_finder.py`
- `tests/test_traceback_media_token.py`

