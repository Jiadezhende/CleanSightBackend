# 基于进程池的FFmpeg解码器架构

## 概述

本项目已从原有的线程模式升级为基于进程池的解码架构，充分利用16核CPU的多核性能，实现高效的视频流拉取和解码。

## 架构设计

### 核心组件

1. **DecoderPool (解码器进程池)**
   - 管理最多16个独立的解码器进程
   - 每个进程运行独立的FFmpeg实例进行视频流拉取和解码
   - 使用multiprocessing.Queue实现进程间帧数据传输

2. **FrameDispatcher (帧分发器)**
   - 在独立线程中运行
   - 从进程池的共享队列获取解码后的帧
   - 将帧分发给AI服务进行推理处理

3. **Decoder Worker (解码器工作进程)**
   - 每个进程独立处理一个视频流
   - 使用FFmpeg进行视频拉流和解码
   - 支持RTMP和RTSP协议
   - 自动处理帧格式标准化

### 架构优势

相比原有的线程模式，新架构具有以下优势：

1. **真正的并行处理**
   - Python的GIL限制不影响进程池
   - 16个进程可以真正并行运行在16个CPU核心上
   - 每个流的解码互不干扰

2. **更好的资源隔离**
   - 进程崩溃不影响其他流
   - 内存独立，避免相互影响
   - FFmpeg实例完全隔离

3. **更高的吞吐量**
   - 可以同时处理最多16个视频流
   - 每个流都获得独立的CPU资源
   - 队列缓冲机制避免丢帧

4. **更灵活的扩展**
   - 可以根据CPU核心数调整进程池大小
   - 支持动态启动和停止解码器

## 文件结构

```
app/
├── services/
│   ├── decoder.py          # 解码器进程池核心实现
│   └── ai.py               # AI推理服务 (接收解码后的帧)
├── routers/
│   └── inspection.py       # API路由 (使用解码器进程池)
test/
└── test_decoder_pool.py    # 解码器进程池测试脚本
```

## API使用

### 启动RTSP流捕获

```bash
POST /inspection/start_rtsp_stream
Content-Type: application/json

{
  "client_id": "camera_001",
  "rtsp_url": "rtsp://localhost:8554/live/stream1",
  "fps": 30
}
```

响应示例：
```json
{
  "status": "success",
  "message": "RTSP 流捕获已启动 for camera_001 (进程池模式)",
  "pool_stats": {
    "total_processes": 1,
    "alive_processes": 1,
    "max_workers": 16,
    "queue_size": 0,
    "clients": ["camera_001"]
  }
}
```

### 停止流捕获

```bash
POST /inspection/stop_rtsp_stream?client_id=camera_001
```

或使用通用接口：
```bash
POST /inspection/stop_stream?client_id=camera_001
```

### 获取进程池统计信息

```bash
GET /inspection/decoder_stats
```

响应示例：
```json
{
  "total_processes": 3,
  "alive_processes": 3,
  "max_workers": 16,
  "queue_size": 45,
  "clients": ["camera_001", "camera_002", "camera_003"]
}
```

## 核心代码说明

### 1. 解码器工作进程 (_decoder_worker)

每个工作进程的主要流程：

```python
def _decoder_worker(client_id, stream_url, protocol, fps, frame_queue, stop_event):
    # 1. 查找FFmpeg可执行文件
    ffmpeg_path = _find_ffmpeg()
    
    # 2. 构建FFmpeg命令
    cmd = [ffmpeg_path, "-i", stream_url, "-f", "rawvideo", ...]
    
    # 3. 启动FFmpeg子进程
    process = subprocess.Popen(cmd, stdout=PIPE, stderr=PIPE)
    
    # 4. 循环读取原始视频数据
    while not stop_event.is_set():
        chunk = process.stdout.read(32768)
        buffer += chunk
        
        # 5. 解析完整帧并标准化
        frame = np.frombuffer(frame_data, dtype=np.uint8).reshape((480, 640, 3))
        std_frame = _standardize_frame(frame)
        
        # 6. 发送到共享队列
        frame_queue.put((client_id, std_frame))
```

### 2. 帧分发器 (FrameDispatcher)

在主进程中的独立线程运行：

```python
class FrameDispatcher:
    def _dispatch_loop(self):
        while self._running:
            # 从进程池队列获取帧
            result = self.decoder_pool.get_frame(timeout=0.1)
            
            if result:
                client_id, frame = result
                # 调用AI服务处理
                self.frame_callback(client_id, frame)
```

### 3. 进程池管理 (DecoderPool)

主进程池管理器：

```python
class DecoderPool:
    def start_decoder(self, client_id, stream_url, protocol, fps):
        # 创建新进程
        process = Process(
            target=_decoder_worker,
            args=(client_id, stream_url, protocol, fps, 
                  self.frame_queue, stop_event)
        )
        process.start()
        
    def stop_decoder(self, client_id):
        # 发送停止信号
        self.stop_events[client_id].set()
        # 等待进程结束
        process.join(timeout=5.0)
```

## 配置选项

### 环境变量

- `FFMPEG_PATH`: FFmpeg可执行文件路径
- `MODEL_INPUT_WIDTH`: 模型输入宽度 (默认: 0，不缩放)
- `MODEL_INPUT_HEIGHT`: 模型输入高度 (默认: 0，不缩放)
- `MODEL_INPUT_COLOR`: 颜色空间 ('bgr' 或 'rgb', 默认: 'bgr')

### 代码配置

在 `decoder.py` 中修改：

```python
PROCESS_POOL_SIZE = 16  # 最大进程数，根据CPU核心数调整
```

在 `DecoderPool.__init__` 中修改：

```python
self.frame_queue = Queue(maxsize=1000)  # 队列大小
```

## 性能优化建议

1. **进程池大小**
   - 建议设置为 CPU 核心数
   - 16核CPU设置为16个进程
   - 留出1-2个核心给系统和其他任务

2. **队列大小**
   - 默认1000帧可缓冲约33秒 (30fps)
   - 根据实际延迟要求调整
   - 队列满时会自动丢弃旧帧

3. **FFmpeg参数**
   - RTSP使用UDP传输 (`-rtsp_transport udp`)
   - 低延迟配置 (`-fflags nobuffer -flags low_delay`)
   - 根据网络条件调整缓冲参数

4. **帧预处理**
   - 在解码器进程中完成帧标准化
   - 减少主进程的计算负担
   - 使用连续内存 (`np.ascontiguousarray`)

## 测试

运行测试脚本：

```bash
cd test
python test_decoder_pool.py
```

测试包括：
1. 获取解码器统计信息
2. 启动/停止单个流
3. 检查运行状态
4. 多流并发压力测试

## 故障排查

### 问题1: 进程无法启动

**症状**: 调用start_decoder返回False

**可能原因**:
- 已达到最大进程数限制
- client_id已存在

**解决方案**:
- 检查 `decoder_stats` 确认当前进程数
- 先停止不需要的流
- 使用唯一的client_id

### 问题2: 帧不更新

**症状**: AI服务没有收到新帧

**可能原因**:
- FrameDispatcher未启动
- FFmpeg无法连接到流
- 进程崩溃

**解决方案**:
- 检查后台日志
- 验证RTSP/RTMP URL是否可访问
- 查看进程池统计中的alive_processes

### 问题3: 内存占用过高

**症状**: 系统内存不断增长

**可能原因**:
- 队列积压过多帧
- 帧未被及时消费
- 内存泄漏

**解决方案**:
- 检查 `queue_size` 是否持续增长
- 确保FrameDispatcher正常运行
- 减小队列maxsize
- 优化AI推理速度

## 迁移指南

### 从旧版本线程模式迁移

原有代码：
```python
# 旧: 线程模式
thread = threading.Thread(
    target=_stream_capture_worker,
    args=(client_id, stream_url, fps, stop_event)
)
thread.start()
```

新代码：
```python
# 新: 进程池模式
decoder_pool = get_decoder_pool()
decoder_pool.start_decoder(
    client_id=client_id,
    stream_url=stream_url,
    protocol="RTSP",
    fps=fps
)
```

### API兼容性

所有现有的 `/inspection/*` API 端点保持兼容，只是内部实现改为进程池。

## 未来改进

1. **动态进程池调整**
   - 根据系统负载自动增减进程数
   - 空闲进程回收机制

2. **GPU加速解码**
   - 支持FFmpeg的硬件加速选项
   - NVDEC/VAAPI集成

3. **更智能的帧丢弃策略**
   - 基于帧重要性的选择性丢弃
   - 关键帧优先保留

4. **分布式部署**
   - 跨机器的进程池
   - 负载均衡和故障转移

## 参考资料

- [FFmpeg文档](https://ffmpeg.org/documentation.html)
- [Python multiprocessing](https://docs.python.org/3/library/multiprocessing.html)
- [RTSP/RTMP协议说明](../docs/RTSP_FLOW.md)
