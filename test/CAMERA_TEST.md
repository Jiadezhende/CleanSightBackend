# 摄像头快速测试指南

## 一键测试（推荐）

```bash
# 1. 启动后端服务
uvicorn app.main:app --reload

# 2. 在新终端运行快速测试
python test/quick_camera_test.py
```

就这么简单！脚本会自动：
- ✓ 检测后端服务
- ✓ 检测可用摄像头
- ✓ 启动摄像头预览
- ✓ 显示实时画面

## 完整测试（包含 AI 推理）

需要 RTMP 服务器支持。

### 步骤 1: 安装 MediaMTX（推荐）

**Windows:**
```bash
# 下载并运行
https://github.com/bluenviron/mediamtx/releases/latest
# 解压后直接运行 mediamtx.exe
```

**Linux/macOS:**
```bash
# 下载
wget https://github.com/bluenviron/mediamtx/releases/download/v1.4.0/mediamtx_v1.4.0_linux_amd64.tar.gz
tar -xzf mediamtx_v1.4.0_linux_amd64.tar.gz

# 运行
./mediamtx
```

### 步骤 2: 推流摄像头

**选项 A: 使用 FFmpeg（需要先安装）**
```bash
# Windows 查看摄像头列表
ffmpeg -list_devices true -f dshow -i dummy

# 推流
ffmpeg -f dshow -i video="Integrated Camera" -f flv rtmp://localhost:1935/live/test
```

**选项 B: 使用 Python 脚本**
```bash
python test/camera_to_rtmp.py --camera_id 0 --rtmp_url rtmp://localhost:1935/live/test
```

### 步骤 3: 测试完整流程

```bash
python test/quick_camera_test.py --rtmp_url rtmp://localhost:1935/live/test --duration 60
```

## 参数说明

```bash
python test/quick_camera_test.py \
  --camera_id 0                              # 摄像头 ID（0=默认）
  --client_id my_camera                      # 客户端标识
  --rtmp_url rtmp://localhost:1935/live/test # RTMP 流地址
  --fps 30                                   # 帧率
  --duration 60                              # 测试时长（秒）
  --skip-rtmp                                # 跳过 RTMP（仅预览）
```

## 查看可用摄像头

```bash
python -c "import cv2; print([i for i in range(5) if cv2.VideoCapture(i).isOpened()])"
```

## 常见问题

### Q: 摄像头无法打开？
```bash
# 检查摄像头是否被占用
# 关闭其他使用摄像头的程序（如 Teams、Zoom）
```

### Q: 后端连接失败？
```bash
# 确保后端服务正在运行
uvicorn app.main:app --reload

# 访问测试
curl http://localhost:8000/
```

### Q: 没有看到推理结果？
```bash
# 检查 AI 服务状态
curl http://localhost:8000/ai/status

# 确保 RTMP 捕获已启动
```

## 测试结果

成功后会在以下位置生成文件：
```
database/
  {client_id}/
    {task_id}/
      hls/
        raw_playlist.m3u8           # 原始视频播放列表
        raw_segment_*.mp4           # 原始视频段
        processed_playlist.m3u8     # 处理后视频播放列表
        processed_segment_*.mp4     # 处理后视频段（含 AI 标注）
        keypoints_*.json            # 关键点数据
```

## 下一步

测试成功后可以：
1. 查看生成的视频文件
2. 分析关键点 JSON 数据
3. 集成真实 AI 模型
4. 部署到生产环境
