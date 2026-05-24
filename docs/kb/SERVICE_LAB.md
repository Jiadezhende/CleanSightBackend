> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Lab Service

Lab 服务用于从 raw HLS 段中裁剪样本视频，并提交到 Label Studio 创建标注任务。

## 路由

- `POST /lab-f3m8/submit`
- `GET /lab-f3m8/health`
- `GET /lab-f3m8/config`
- `PUT /lab-f3m8/config`
- 静态 UI：`/lab-f3m8/ui`

路径使用 `lab-f3m8`，代码注释说明目的是降低自动扫描器命中率。

## Submit 流程

1. 校验 Label Studio URL/token 配置。
2. 解析 project_id，请求优先，其次使用默认配置。
3. 校验 clips 数量、单段时长、总时长和不重叠。
4. 检查该 task/step 是否存在 raw segments。
5. 在线程池中同步执行 ffmpeg 裁剪和 Label Studio 上传。
6. 单段失败不让整请求失败，响应中逐段标记 success/error_code。

## ClipBuilder

ClipBuilder 使用 raw 轨：

1. 通过 SegmentFinder 找到与 `[start_ms, end_ms]` 重叠的 raw 段。
2. 校验相邻段间隙。
3. 构造临时 HLS m3u8，让 ffmpeg HLS demuxer 读取 `init.mp4 + fragments`。
4. 输出端重编码 libx264，获得 ms 级裁剪 mp4。

代码明确说明不使用 concat demuxer，因为 fMP4 fragment 单独 demux 时缺 codec init。

## Label Studio Client

当前实现是极简 urllib 客户端：

- `ping()`：GET `/api/version`
- `import_clip()`：POST `/api/projects/{project_id}/import`

multipart 会把整个 mp4 读入内存。注释说明 Lab 场景下 clip 通常小于 5 分钟，可以接受。

## 配置

可在页面持久化：

- Label Studio URL
- 默认 project_id

只能通过环境变量配置：

- Label Studio token

## 代码来源

- `app/routers/lab.py`
- `app/services/lab/clip_builder.py`
- `app/services/lab/label_studio_client.py`
- `app/services/lab/runtime_config.py`
- `app/static/lab/index.html`
- `tests/test_lab_clip_builder.py`

