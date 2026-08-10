# 视频导出 & 上传操作步骤

> ## ⚠️ 本流程已失效（2026-08-10）
>
> 第二步的 [concat_and_upload.py](concat_and_upload.py) **在当前落盘格式下必然失败或产出坏视频**，两条都是硬伤：
>
> | 问题 | 后果 |
> |------|------|
> | 目录假设 `{client_id}/{task_id}/` | 实际是 `{task_id}/{step_id}/`，扫不到任何段 |
> | 用 `ffmpeg -f concat -c copy` | 段是 fMP4 fragment、**无 moov box**，concat demuxer 读不了；须走 HLS demuxer 吃 `#EXT-X-MAP:URI="init.mp4"` |
>
> 即使手工修正上面两条，还有第三个坑：写入侧 `{track}_playlist.m3u8` **不写 `#EXT-X-ENDLIST`**，
> ffmpeg 会当直播流只读 live edge，前面的段全丢——必须自己补 ENDLIST。
>
> **改用接口**（三个坑都已封装，服务端纯 `-c copy` remux，不重编码）：
>
> ```bash
> curl -sS -o out.mp4 \
>   "http://<host>:8000/lab-f3m8/download?task_id=<task>&step_id=<step>&track=processed"
> ```
>
> 或直接在送标面板 `http://<host>:8000/lab-f3m8/ui/` 上点「⬇ 下载整段」。
> 契约见 [docs/api/lab.md](../../docs/api/lab.md)，实现说明见 [docs/update/20260810_LAB_STEP_DOWNLOAD.md](../../docs/update/20260810_LAB_STEP_DOWNLOAD.md)。
>
> 以下内容仅作历史留存；`rsync`（第一步）与上传（第三步）部分仍可参考。

## 网络拓扑

```text
测试机 ──(公网/其他网段)──> 本机 ──(校园网)──> 10.176.122.22（标注服务器）
```

测试机与标注服务器**不在同一内网**，因此必须以本机为中转：

1. 从测试机拉取视频段到本机
2. 本机拼接
3. 本机上传到标注服务器（需接入校园网）

## 前置条件

- 本机已安装 ffmpeg（`ffmpeg -version` 验证）
- 本机已接入校园网（或校园 VPN），可访问 10.176.122.22
- 已知测试机 SSH 账号 / 标注服务器 root 密码

---

## 第一步：从测试机拉取 raw 视频段

```bash
# rsync 仅拉取 raw 相关文件（节省带宽，跳过 processed 段）
rsync -avhP \
    --include='*/' \
    --include='raw_segment_*.mp4' \
    --include='raw_playlist.m3u8' \
    --include='metadata.json' \
    --exclude='*' \
    ubuntu@106.75.229.120:/home/ubuntu/CleanSightBackend/database/ \
    ./database_export/
```

如果测试机没有 rsync，用 scp 全量拉取：

```bash
scp -r ubuntu@106.75.229.120:/home/ubuntu/CleanSightBackend/database/ ./database_export/
```

---

## 第二步：本机拼接

```bash
# 拼接所有任务的 raw 视频
python scripts/video_export/concat_and_upload.py \
    --db-dir ./database_export \
    --output-dir ./merged_videos

# 只拼接指定 task_id（如 12、15、20）
python scripts/video_export/concat_and_upload.py \
    --db-dir ./database_export \
    --output-dir ./merged_videos \
    --task-ids 12 15 20

# 查看拼接结果
ls -lh merged_videos/
```

---

## 第三步：上传到标注服务器（需在校园网内）

```bash
# 首次：在标注服务器上建好目标目录
ssh root@10.176.122.22 "mkdir -p /data/annotation/videos"

# 上传（rsync 支持断点续传，推荐）
rsync -avhP merged_videos/*.mp4 root@10.176.122.22:/data/annotation/videos/
```

---

## 常用脚本参数说明

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--db-dir` | database 根目录 | 必填 |
| `--output-dir` | 拼接结果输出目录 | 必填 |
| `--seg-type` | `raw` / `processed` / `both` | `raw` |
| `--task-ids` | 只处理指定 task_id | 全部 |

---

## 输出文件命名规则

```text
{client_id}__task{task_id}__raw.mp4
例：camera01__task12__raw.mp4
```

---

## 故障排查

### ffmpeg 未安装（本机）

```bash
# macOS
brew install ffmpeg
# Ubuntu/Debian
apt-get install -y ffmpeg
# Windows（推荐 winget）
winget install ffmpeg
```

### scp/rsync 每次都要输密码

```bash
# 配置免密登录（一次性）
ssh-copy-id root@10.176.122.22
ssh-copy-id ubuntu@106.75.229.120
```

### 某个视频段损坏导致 ffmpeg 报错

```bash
# 检查具体哪个段有问题
ffprobe database_export/<client>/<task>/raw_segment_xxx.mp4
# 脚本会跳过缺失文件，损坏文件需手动删除后重跑
```
