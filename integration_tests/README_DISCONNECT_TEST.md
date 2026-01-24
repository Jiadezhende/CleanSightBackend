# 推流断开重连测试

## 功能说明

该脚本模拟视频推流的断开与重连场景，用于测试系统在网络中断后的恢复能力。

## 测试流程

1. **推流 15 秒** - 建立正常的 RTSP 推流
2. **中断 5 秒** - 停止推流，模拟网络断开
3. **重新连接** - 重新建立推流连接
4. **再推流 15 秒** - 验证重连后系统正常工作
5. **结束测试** - 清理所有资源

## 使用方法

### 基本用法

```bash
python integration_tests/test_stream_disconnect_reconnect.py
```

### 指定参数

```bash
# 指定任务 ID
python integration_tests/test_stream_disconnect_reconnect.py --task_id 1

# 指定测试视频路径
python integration_tests/test_stream_disconnect_reconnect.py --video_path /path/to/video.mp4

# 组合使用
python integration_tests/test_stream_disconnect_reconnect.py --task_id 2 --video_path test/custom_video.mp4
```

## 前置条件

1. **后端服务运行中** - 确保 CleanSight 后端 API 在 `http://localhost:8000` 运行
2. **MediaMTX 运行中** - 确保 RTSP 服务器在 `rtsp://localhost:8004` 可用
3. **测试视频存在** - 默认使用 `test/test_video.mp4`
4. **FFmpeg 已安装** - 系统需要安装 FFmpeg

## 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--task_id` | int | 1 | 数据库中的任务 ID |
| `--video_path` | str | test/test_video.mp4 | 测试视频文件路径 |

## 输出示例

```
🚀 推流断开重连测试
============================================================

[1/7] 检查前置条件...
✅ 后端 API 正常
✅ 测试视频存在: test/test_video.mp4

[2/7] 准备测试任务...
✅ 任务 1 已存在
✅ Client ID: rtsp.test.1
✅ RTSP URL: rtsp://localhost:8004/live/rtsp.test.1

[3/7] 启动后端任务...
✅ 任务 1 已加载到 AI 服务

[4/7] 开始第一次推流（15秒）...
✅ ffmpeg RTSP 推流已启动: rtsp://localhost:8004/live/rtsp.test.1
📡 启动 RTSP 捕获...
✅ RTSP 捕获已启动
⏱️  推流中... (15秒)

[5/7] ⚠️  中断推流（5秒）...
✅ ffmpeg 推流已停止
⏱️  等待中... (5秒)

[6/7] 🔄 重新连接推流...
✅ ffmpeg RTSP 推流已启动: rtsp://localhost:8004/live/rtsp.test.1
⏱️  推流中... (15秒)

[7/7] ✅ 测试完成，清理资源...

🧹 清理资源...
✅ FFmpeg 已停止
✅ RTSP 捕获已停止

============================================================
✅ 测试结束
```

## 注意事项

- 该脚本不包含自动验证逻辑，仅模拟断开重连场景
- 可以配合日志和监控工具观察系统行为
- 测试期间可以通过 Ctrl+C 随时中断
