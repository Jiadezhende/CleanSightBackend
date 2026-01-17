# 服务启动时初始化解码器进程池

## 更新日期
2026年1月17日

## 修改内容

将解码器进程池从**懒加载**（首次使用时初始化）改为**启动时初始化**，确保服务启动后立即可用。

## 修改文件

### 1. [main.py](../app/main.py)

**修改内容：**
- 在 `lifespan` 上下文管理器中添加解码器进程池初始化
- 在启动时创建进程池和帧分发器
- 在关闭时清理进程池和帧分发器

**修改前：**
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # AI服务的生命周期由ai路由器管理
    async with ai.lifespan():
        yield
```

**修改后：**
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 1. 启动时：初始化解码器进程池
    print("[Startup] 初始化解码器进程池...")
    decoder_pool = get_decoder_pool()
    print(f"[Startup] 解码器进程池已初始化 (最大进程数: {decoder_pool.max_workers})")
    
    # 2. 启动时：启动帧分发器
    print("[Startup] 启动帧分发器...")
    start_frame_dispatcher(ai_service.submit_frame)
    print("[Startup] 帧分发器已启动")
    
    # 3. AI服务的生命周期由ai路由器管理
    async with ai.lifespan():
        yield
    
    # 4. 关闭时：停止帧分发器
    print("[Shutdown] 停止帧分发器...")
    stop_frame_dispatcher()
    
    # 5. 关闭时：关闭解码器进程池
    print("[Shutdown] 关闭解码器进程池...")
    shutdown_decoder_pool()
    print("[Shutdown] 清理完成")
```

### 2. [inspection.py](../app/routers/inspection.py)

**修改内容：**
- 移除 `_dispatcher_started` 全局变量和锁
- 移除 `_ensure_dispatcher_started()` 函数
- 简化 `start_rtmp_stream` 和 `start_rtsp_stream` 函数

**原因：** 由于进程池和帧分发器已在启动时初始化，不需要再进行懒加载检查。

### 3. 新增测试文件

**[test_startup_initialization.py](../test/test_startup_initialization.py)**
- 验证进程池在启动时正确初始化
- 验证帧分发器已启动
- 验证关闭时正确清理

## 启动流程

```
应用启动 → lifespan.__aenter__
  ↓
1. 初始化解码器进程池
   - 创建DecoderPool实例（最大16个进程）
   - 创建共享帧队列
  ↓
2. 启动帧分发器
   - 创建FrameDispatcher实例
   - 启动分发线程
   - 连接到AI服务的submit_frame
  ↓
3. 启动AI服务
   - 推理服务启动
  ↓
应用运行中...
  ↓
应用关闭 → lifespan.__aexit__
  ↓
4. 停止AI服务
  ↓
5. 停止帧分发器
   - 停止分发线程
  ↓
6. 关闭解码器进程池
   - 停止所有运行中的解码进程
   - 清理资源
  ↓
应用关闭完成
```

## 启动日志示例

```
[Startup] 初始化解码器进程池...
[DecoderPool] 初始化，最大进程数: 16
[Startup] 解码器进程池已初始化 (最大进程数: 16)
[Startup] 启动帧分发器...
[FrameDispatcher] 分发循环启动
[FrameDispatcher] 已启动
[Startup] 帧分发器已启动
AI 推理服务已启动（多客户端管理：RT/CA 队列）
```

## 关闭日志示例

```
[Shutdown] 停止帧分发器...
[FrameDispatcher] 分发循环退出
[FrameDispatcher] 已停止
[Shutdown] 关闭解码器进程池...
[DecoderPool] 停止所有解码器...
[DecoderPool] 已停止所有解码器
[Shutdown] 清理完成
```

## 优势

### 1. 即时可用
- 服务启动后立即可以接收流捕获请求
- 无需等待首次请求时的初始化延迟

### 2. 更好的资源管理
- 统一的生命周期管理
- 确保关闭时正确清理资源
- 避免资源泄漏

### 3. 更清晰的架构
- 初始化逻辑集中在一处
- 各路由不需要关心初始化细节
- 代码更简洁

### 4. 更好的可测试性
- 可以独立测试启动和关闭流程
- 便于集成测试

## API使用

API使用方式**完全不变**，但现在：
- ✅ 无需担心首次请求延迟
- ✅ 服务启动后立即可用
- ✅ 更可靠的资源清理

```bash
# 启动服务后立即可用
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 无需等待，直接调用
curl -X POST http://localhost:8000/inspection/start_rtsp_stream \
  -H "Content-Type: application/json" \
  -d '{"client_id": "cam1", "rtsp_url": "rtsp://...", "fps": 30}'
```

## 验证方法

### 1. 运行启动测试
```bash
python test/test_startup_initialization.py
```

### 2. 启动服务并检查日志
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

查看启动日志中是否有：
```
[Startup] 初始化解码器进程池...
[Startup] 启动帧分发器...
```

### 3. 立即测试API
服务启动后立即调用API，验证无延迟：
```bash
curl http://localhost:8000/inspection/decoder_stats
```

应返回：
```json
{
  "total_processes": 0,
  "alive_processes": 0,
  "max_workers": 16,
  "queue_size": 0,
  "clients": []
}
```

## 注意事项

1. **Windows平台**: multiprocessing需要正确的模块导入保护
2. **进程池大小**: 固定为16，适合16核CPU
3. **清理顺序**: 先停止帧分发器，再关闭进程池

## 测试结果

✅ 测试通过：
- 进程池在启动时正确初始化（16个工作进程）
- 帧分发器已启动
- 关闭时正确清理所有资源

## 相关文档

- [解码器进程池架构](DECODER_POOL_ARCHITECTURE.md)
- [快速开始指南](DECODER_POOL_QUICKSTART.md)
- [升级总结](DECODER_POOL_UPGRADE_SUMMARY.md)
