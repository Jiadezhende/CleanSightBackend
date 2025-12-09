# 远程服务器测试框架

这是一个用于测试部署在远程服务器上的CleanSight后端服务的集成测试框架。

## 功能特性

- **远程推流**: 使用ffmpeg向远程服务器推送RTMP视频流
- **任务管理**: 自动加载和终止远程服务器上的AI检测任务
- **实时监控**: 通过WebSocket实时接收AI推理结果和任务状态
- **可视化显示**: 本地显示远程服务器的AI处理结果
- **自动化测试**: 完整的端到端测试流程

## 测试流程

1. **推流阶段**: ffmpeg推送测试视频到远程服务器的RTMP服务
2. **任务加载**: 调用`/ai/load_task/{task_id}`加载预设任务(task_id=0)
3. **流捕获启动**: 调用`/inspection/start_rtmp_stream`启动RTMP流捕获
4. **实时监控**: 
   - 通过`/ai/video` WebSocket接收AI处理后的视频帧
   - 通过`/task/status/{client_id}` WebSocket接收任务状态更新
5. **本地可视化**: 实时显示远程AI推理结果和统计信息
6. **任务终止**: 调用`/ai/terminate_task/{client_id}`清理资源

## 使用方法

### 方式一：使用PowerShell脚本 (推荐)

```powershell
# 基本用法
.\integration_tests\run_remote_test.ps1 -Server 192.168.1.100

# 自定义参数
.\integration_tests\run_remote_test.ps1 -Server 192.168.1.100 -Duration 120 -TaskId 0 -ClientId "my_test"
```

### 方式二：直接使用Python脚本

```bash
# 激活虚拟环境
.\.venv\Scripts\Activate.ps1

# 运行测试
python integration_tests/remote_test_pipeline.py --server 192.168.1.100 --duration 60

# 查看所有选项
python integration_tests/remote_test_pipeline.py --help
```

## 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--server` / `-s` | 远程服务器IP地址 | **必需** |
| `--task_id` | 要加载的任务ID | 0 |
| `--client_id` | 客户端标识符 | remote_test_client |
| `--duration` / `-d` | 测试时长（秒） | 60 |
| `--video_path` | 测试视频路径 | test/test_video.mp4 |

## 前置要求

### 本地环境

1. **Python环境**: Python 3.8+
2. **依赖包**: 
   - opencv-python
   - websockets  
   - requests
3. **FFmpeg**: 用于视频推流
4. **测试视频**: `test/test_video.mp4`

### 远程服务器

1. **CleanSight后端服务**: 运行在8000端口
2. **nginx-rtmp服务**: 运行在1935端口  
3. **数据库**: 包含task_id=0的预设任务
4. **防火墙**: 开放8000和1935端口

## 可视化界面

测试期间会显示实时视频窗口，包含以下信息：

- **服务器信息**: IP地址、任务ID、客户端ID
- **实时统计**: 接收帧数、FPS、状态更新次数、运行时间
- **AI推理结果**: 远程服务器处理后的视频帧

### 交互控制

- `q` 键: 退出测试
- `c` 键: 关闭/重新打开视频窗口

## 输出信息

### 控制台输出

```
🌐 CleanSight 远程服务器测试
================================================================================
服务器IP: 192.168.1.100
任务ID: 0
客户端ID: remote_test_client
测试时长: 60 秒
RTMP推流地址: rtmp://192.168.1.100:1935/live/remote_test
API地址: http://192.168.1.100:8000
================================================================================

📋 步骤 1: 检查前置条件
--------------------------------------------------------------------------------
✅ ffmpeg: C:\ffmpeg\bin\ffmpeg.exe
✅ 测试视频: test\test_video.mp4
✅ 远程API连接: http://192.168.1.100:8000
   服务状态: running

📋 步骤 2: 启动 ffmpeg 推流到远程服务器
--------------------------------------------------------------------------------
✅ ffmpeg 推流已启动: rtmp://192.168.1.100:1935/live/remote_test
⏳ 等待推流稳定 (10 秒)...

📋 步骤 3: 加载任务 (task_id=0)
--------------------------------------------------------------------------------
✅ 任务加载成功: {'task_id': 0, 'status': 'running', 'cleaning_stage': '1'}

📋 步骤 4: 启动 RTMP 流捕获
--------------------------------------------------------------------------------
✅ RTMP 流捕获启动成功: {'status': 'success', 'client_id': 'remote_test_client'}

📋 步骤 5: 实时监控和可视化 (60 秒)
--------------------------------------------------------------------------------
按 'q' 键退出实时监控
按 'c' 键关闭/打开可视化窗口
✅ 视频流 WebSocket 连接成功
✅ 状态流 WebSocket 连接成功

📊 状态更新 #1:
   任务ID: 0
   状态: 任务运行中
   清洗步骤: 步骤1：预浸润
   弯折: False
   气泡: False
   浸没: True

📈 运行时间: 25.3s | 帧数: 756 | FPS: 29.9 | 状态更新: 76
```

### 测试报告

```
📊 远程测试报告
================================================================================
测试服务器: 192.168.1.100
测试时长: 60.2 秒
接收帧数: 1804
平均FPS: 29.97
状态更新: 181 次

✅ 成功步骤:
  - ffmpeg 推流到远程服务器
  - 加载任务
  - 实时视频流接收
  - 实时状态监控
  - 任务终止

🎉 远程测试成功完成！
================================================================================
```

## 故障排查

### 常见问题

1. **无法连接远程API**
   ```
   ❌ 无法连接远程API: Connection refused
   ```
   - 检查服务器IP地址是否正确
   - 确认远程服务器8000端口开放
   - 检查CleanSight后端服务是否运行

2. **RTMP推流失败**
   ```
   ❌ ffmpeg 推流进程已退出 (退出码: 1)
   ```
   - 确认远程服务器1935端口开放
   - 检查nginx-rtmp服务状态
   - 验证网络连接

3. **WebSocket连接失败**
   ```
   ❌ 视频流 WebSocket 连接失败: Connection refused
   ```
   - 检查防火墙设置
   - 确认WebSocket协议支持
   - 验证client_id参数

4. **任务加载失败**
   ```
   ❌ 加载任务失败: Task 0 not found
   ```
   - 检查数据库中是否存在task_id=0的记录
   - 确认数据库连接正常
   - 验证任务状态

### 调试命令

```bash
# 检查远程API状态
curl http://192.168.1.100:8000/ai/status

# 检查nginx-rtmp统计
curl http://192.168.1.100/stat

# 测试RTMP连接
ffmpeg -f lavfi -i testsrc=duration=10:size=320x240:rate=30 -f flv rtmp://192.168.1.100:1935/live/test
```

## 技术架构

```
┌─────────────────┐    RTMP流     ┌─────────────────────┐
│   本地客户端    │ ────────────► │   远程服务器        │
│                 │               │                     │
│ ┌─────────────┐ │               │ ┌─────────────────┐ │
│ │   ffmpeg    │ │               │ │  nginx-rtmp     │ │
│ └─────────────┘ │               │ └─────────────────┘ │
│                 │               │          │          │
│ ┌─────────────┐ │  HTTP API     │ ┌─────────▼───────┐ │
│ │ Python测试  │ │ ◄──────────── │ │ CleanSight后端  │ │
│ │   框架      │ │  WebSocket    │ │   (FastAPI)     │ │
│ └─────────────┘ │ ◄──────────── │ └─────────────────┘ │
│                 │               │          │          │
│ ┌─────────────┐ │               │ ┌─────────▼───────┐ │
│ │ OpenCV可视  │ │               │ │   AI推理服务    │ │
│ │   化显示    │ │               │ │                 │ │
│ └─────────────┘ │               │ └─────────────────┘ │
└─────────────────┘               └─────────────────────┘
```

## 示例配置

### PowerShell执行策略设置

如果遇到PowerShell执行策略问题：

```powershell
# 设置执行策略(一次性)
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# 或者绕过策略运行
powershell -ExecutionPolicy Bypass -File .\integration_tests\run_remote_test.ps1 -Server 192.168.1.100
```

### 防火墙配置示例

**Windows防火墙**:
```cmd
netsh advfirewall firewall add rule name="CleanSight API" dir=in action=allow protocol=TCP localport=8000
netsh advfirewall firewall add rule name="CleanSight RTMP" dir=in action=allow protocol=TCP localport=1935
```

**Linux iptables**:
```bash
sudo iptables -A INPUT -p tcp --dport 8000 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 1935 -j ACCEPT
```

## 性能建议

- **网络带宽**: 建议至少10Mbps上行带宽用于RTMP推流
- **延迟要求**: 推荐网络延迟 < 100ms 获得最佳体验  
- **测试时长**: 建议测试时长60-300秒，过短可能无法充分验证系统稳定性
- **并发测试**: 避免同时运行多个测试实例，可能导致资源竞争