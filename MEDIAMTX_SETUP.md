# MediaMTX 快速启动指南

## 1. 下载和安装 MediaMTX

### Windows 系统

1. **下载 MediaMTX**
   - 访问：https://github.com/bluenviron/mediamtx/releases
   - 下载最新版本的 Windows 版本：`mediamtx_vX.X.X_windows_amd64.zip`

2. **解压到项目目录**
   ```powershell
   # 建议解压到项目根目录下的 mediamtx_vX.X.X 文件夹
   cd E:\ywc_college\junior1\本科生课题\src\CleanSightBackend
   # 解压后应该有 mediamtx.exe 和 mediamtx.yml 文件
   ```

3. **验证安装**
   ```powershell
   cd mediamtx_vX.X.X
   .\mediamtx.exe --version
   ```

---

## 2. 启动 MediaMTX

### 基本启动（使用默认配置）

```powershell
cd E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\mediamtx_vX.X.X
.\mediamtx.exe
```

**默认配置：**
- RTMP 监听端口：`1935`
- RTSP 监听端口：`8554`
- HLS 监听端口：`8888`
- WebRTC 监听端口：`8889`

**成功启动的输出：**
```
2025/11/29 10:00:00 INF MediaMTX v1.8.0
2025/11/29 10:00:00 INF [RTMP] listener opened on :1935
2025/11/29 10:00:00 INF [RTSP] listener opened on :8554
2025/11/29 10:00:00 INF [HLS] listener opened on :8888
```

---

## 3. 完整测试流程

### 方案：4 个终端窗口

#### 终端 1: 启动 MediaMTX

```powershell
cd E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\mediamtx_vX.X.X
.\mediamtx.exe
```

保持此终端运行。

---

#### 终端 2: 启动后端 API

```powershell
cd E:\ywc_college\junior1\本科生课题\src\CleanSightBackend
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

等待输出：
```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

---

#### 终端 3: 推流测试视频

```powershell
cd E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\test

# 确认测试视频存在
ls test_video.mp4

# 开始推流
ffmpeg -re -i test_video.mp4 -c:v libx264 -preset veryfast -tune zerolatency -f flv rtmp://localhost:1935/live/test
```

**推流成功的标志：**
- ffmpeg 输出包含 `Stream #0:0` 和帧率信息
- MediaMTX 终端显示：`[RTMP] [conn] opened`

---

#### 终端 4: 运行集成测试

```powershell
cd E:\ywc_college\junior1\本科生课题\src\CleanSightBackend\test

# 等待 2-3 秒让推流稳定后运行
python test_rtmp_integration.py --rtmp_url rtmp://localhost:1935/live/test --client_id camera_001 --duration 30
```

**预期输出：**
```
============================================================
RTMP 流捕获和 AI 推理集成测试
============================================================
启动 RTMP 捕获: camera_001 <- rtmp://localhost:1935/live/test
✓ RTMP 捕获已启动: {'message': 'RTMP stream started', 'client_id': 'camera_001'}

AI 服务状态:
  客户端数量: 1
  客户端 camera_001:
    CA-ReadyQueue: 5 帧
    CA-RawQueue: 3 帧
    CA-ProcessedQueue: 2 帧
    RT-ProcessedQueue: 1 帧
    RTMP URL: rtmp://localhost:1935/live/test

连接到 WebSocket: ws://localhost:8000/ai/video?client_id=camera_001
WebSocket 已连接，开始接收推理结果...
已接收 30 帧推理结果
已接收 60 帧推理结果
...
```

---

## 4. 验证流媒体

### 使用 VLC 播放器验证推流

1. 打开 VLC Media Player
2. 媒体 → 打开网络串流
3. 输入：`rtmp://localhost:1935/live/test`
4. 点击播放

如果能看到视频，说明推流成功。

### 使用 ffplay 验证（命令行）

```powershell
ffplay rtmp://localhost:1935/live/test
```

---

## 5. 常见问题排查

### 问题 1: MediaMTX 启动失败 - 端口被占用

**错误信息：**
```
ERR [RTMP] listen tcp :1935: bind: Only one usage of each socket address
```

**解决方法：**
```powershell
# 查找占用端口 1935 的进程
netstat -ano | findstr 1935

# 终止进程（PID 为上一步查到的进程 ID）
taskkill /F /PID <PID>

# 重新启动 MediaMTX
.\mediamtx.exe
```

---

### 问题 2: ffmpeg 推流失败

**错误信息：**
```
[rtmp @ 000001] Cannot open connection tcp://localhost:1935
```

**解决方法：**
1. 确认 MediaMTX 正在运行
2. 检查 MediaMTX 输出中是否有 `[RTMP] listener opened on :1935`
3. 防火墙可能阻止连接，临时关闭防火墙测试

---

### 问题 3: 后端无法捕获 RTMP 流

**症状：** CA-ReadyQueue 始终为 0

**解决方法：**
1. 确认推流正在进行（ffmpeg 没有报错）
2. 检查后端日志：
   ```powershell
   # 在后端终端查看是否有错误
   ```
3. 验证 RTMP URL 是否正确：
   - 推流地址：`rtmp://localhost:1935/live/test`
   - 测试脚本 `--rtmp_url` 参数必须一致

---

## 6. MediaMTX 配置文件（可选）

如果需要修改配置，编辑 `mediamtx.yml`：

```yaml
# mediamtx.yml

# RTMP 配置
rtmp: yes
rtmpAddress: :1935
rtmpEncryption: "no"

# 日志级别
logLevel: info

# 路径配置
paths:
  all:
    # 允许所有来源推流
    source: publisher
    sourceProtocol: automatic
```

修改后重启 MediaMTX：
```powershell
# Ctrl+C 停止当前运行的 MediaMTX
# 重新启动
.\mediamtx.exe
```

---

## 7. 停止所有服务

### 优雅停止顺序

1. **停止测试脚本**（终端 4）
   - 等待测试完成或按 `Ctrl+C`

2. **停止推流**（终端 3）
   - 按 `Ctrl+C` 停止 ffmpeg

3. **停止后端 API**（终端 2）
   - 按 `Ctrl+C`

4. **停止 MediaMTX**（终端 1）
   - 按 `Ctrl+C`

---

## 8. 一键启动脚本（可选）

创建 `start_rtmp_test.ps1`：

```powershell
# start_rtmp_test.ps1
# RTMP 测试一键启动脚本

Write-Host "=== RTMP 测试环境启动脚本 ===" -ForegroundColor Green

# 1. 启动 MediaMTX
Write-Host "`n[1/3] 启动 MediaMTX..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd mediamtx_v1.8.0; .\mediamtx.exe"

Start-Sleep -Seconds 2

# 2. 启动后端 API
Write-Host "[2/3] 启动后端 API..." -ForegroundColor Yellow
Start-Process powershell -ArgumentList "-NoExit", "-Command", ".\.venv\Scripts\Activate.ps1; uvicorn app.main:app --reload"

Start-Sleep -Seconds 3

# 3. 提示手动推流和测试
Write-Host "[3/3] 环境已准备就绪！" -ForegroundColor Green
Write-Host "`n请在新终端中运行以下命令：" -ForegroundColor Cyan
Write-Host ""
Write-Host "# 推流（终端 3）" -ForegroundColor Gray
Write-Host "cd test" -ForegroundColor White
Write-Host "ffmpeg -re -i test_video.mp4 -c:v libx264 -f flv rtmp://localhost:1935/live/test" -ForegroundColor White
Write-Host ""
Write-Host "# 测试（终端 4，等待推流稳定后）" -ForegroundColor Gray
Write-Host "cd test" -ForegroundColor White
Write-Host "python test_rtmp_integration.py --rtmp_url rtmp://localhost:1935/live/test" -ForegroundColor White
```

使用脚本：
```powershell
.\start_rtmp_test.ps1
```

---

## 总结

✅ **已完成配置清单：**
- [x] 下载并解压 MediaMTX
- [x] 了解基本启动命令
- [x] 熟悉完整测试流程（4 个终端）
- [x] 掌握常见问题排查方法

🚀 **下一步：**
按照"3. 完整测试流程"，依次启动 4 个终端窗口开始测试！

💡 **提示：**
建议先用 VLC 或 ffplay 验证推流成功，再运行集成测试脚本。
