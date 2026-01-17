# 解码器进程池升级总结

## 升级日期
2026年1月17日

## 升级内容

本次升级将原有的**线程模式**拉流解码实现改为**基于进程池的架构**，充分利用16核CPU的多核性能。

## 主要变更

### 1. 新增文件

- **app/services/decoder.py** (418行)
  - `DecoderPool`: 进程池管理器
  - `FrameDispatcher`: 帧分发器
  - `_decoder_worker`: 解码器工作进程函数
  - 工具函数: `_find_ffmpeg`, `_standardize_frame`

- **test/test_decoder_pool.py** (254行)
  - 完整的测试套件
  - 包含单流、多流、压力测试

- **docs/DECODER_POOL_ARCHITECTURE.md**
  - 完整的架构文档（400+行）
  - 包含设计原理、API说明、配置指南、故障排查

- **docs/DECODER_POOL_QUICKSTART.md**
  - 快速开始指南（300+行）
  - 包含使用示例、常见场景、性能建议

### 2. 修改文件

- **app/routers/inspection.py**
  - 移除了旧的 `_capture_threads` 和 `_stop_events`
  - 所有流捕获接口改为使用 `DecoderPool`
  - 新增全局 `FrameDispatcher` 管理
  - 新增 `/decoder_stats` 端点

### 3. 保留的代码

- 保留了旧的 `_legacy_stream_capture_worker` 函数（已重命名）
- 所有API端点保持向后兼容

## 架构对比

### 旧架构（线程模式）
```
┌─────────────┐
│  FastAPI    │
│  Router     │
└──────┬──────┘
       │ 创建线程
       ▼
┌─────────────┐     ┌──────────┐
│   Thread    │────>│  FFmpeg  │
│  Worker 1   │     │ Process  │
└─────────────┘     └──────────┘
       │ 直接调用
       ▼
┌─────────────┐
│ AI Service  │
│ submit_frame│
└─────────────┘
```

**限制**:
- Python GIL限制，无法真正并行
- 线程间共享内存，容易相互影响
- 扩展性差

### 新架构（进程池模式）
```
┌─────────────┐
│  FastAPI    │
│  Router     │
└──────┬──────┘
       │ 使用进程池
       ▼
┌─────────────────────────────────┐
│        DecoderPool              │
│  (管理最多16个解码进程)          │
└──┬───┬───┬───────────────────┬──┘
   │   │   │                   │
   ▼   ▼   ▼                   ▼
┌────┐┌────┐┌────┐         ┌────┐
│P 1 ││P 2 ││P 3 │   ...   │P16 │  独立进程
└─┬──┘└─┬──┘└─┬──┘         └─┬──┘
  │     │     │              │
  └─────┴─────┴──────────────┘
             │ 进程间Queue
             ▼
    ┌─────────────────┐
    │FrameDispatcher  │  线程
    │  (分发循环)      │
    └────────┬────────┘
             │ 回调
             ▼
      ┌─────────────┐
      │ AI Service  │
      │submit_frame │
      └─────────────┘
```

**优势**:
- ✅ 真正的多核并行（不受GIL限制）
- ✅ 进程隔离，稳定性高
- ✅ 可扩展到16个并发流
- ✅ 更好的资源管理

## 技术细节

### 1. 多进程管理
- 使用 `multiprocessing.Process` 创建独立进程
- 每个进程运行独立的FFmpeg实例
- 使用 `multiprocessing.Queue` 进行进程间通信
- 使用 `multiprocessing.Event` 进行进程控制

### 2. 帧数据流
```
FFmpeg输出 → 原始字节流 → numpy数组 → 标准化 → Queue → FrameDispatcher → AI Service
```

### 3. 内存管理
- 队列最大1000帧（可配置）
- 队列满时自动丢弃旧帧
- 使用 `np.ascontiguousarray` 确保内存连续性

### 4. 进程生命周期
```
启动: DecoderPool.start_decoder()
  ↓
创建: mp.Event() + mp.Process()
  ↓
运行: _decoder_worker() 循环
  ↓
停止: Event.set() → Process.join() → 清理
```

## 性能指标

### 理论性能
- **最大并发流**: 16个（16核CPU）
- **单流性能**: 30 fps @ 640x480
- **总吞吐量**: 480 fps (16流 × 30fps)
- **延迟**: < 100ms (FFmpeg + 队列)

### 资源占用
- **CPU**: 16个进程，每个约6-12% (共100%左右)
- **内存**: 
  - 进程池: ~1GB (16进程 × 50-100MB)
  - 帧队列: ~1GB (1000帧 × 1MB)
  - 总计: ~2GB

### 网络带宽
- 720p@30fps: ~2-5 Mbps/流
- 16流: ~30-80 Mbps总带宽

## API变化

### 新增端点
```
GET /inspection/decoder_stats
```

### 修改端点（内部实现改变，接口不变）
```
POST /inspection/start_rtsp_stream
POST /inspection/start_rtmp_stream  
POST /inspection/stop_rtsp_stream
POST /inspection/stop_rtmp_stream
POST /inspection/stop_stream
```

### 响应变化
所有启动/停止接口现在返回 `pool_stats`:
```json
{
  "status": "success",
  "message": "...",
  "pool_stats": {
    "total_processes": 1,
    "alive_processes": 1,
    "max_workers": 16,
    "queue_size": 45,
    "clients": ["camera_001"]
  }
}
```

## 配置选项

### 代码配置
```python
# decoder.py
PROCESS_POOL_SIZE = 16        # 最大进程数
frame_queue = Queue(maxsize=1000)  # 队列大小
```

### 环境变量
```bash
FFMPEG_PATH           # FFmpeg路径
MODEL_INPUT_WIDTH     # 模型输入宽度
MODEL_INPUT_HEIGHT    # 模型输入高度
MODEL_INPUT_COLOR     # 颜色空间 (bgr/rgb)
```

## 测试覆盖

### 单元测试
- ✅ 解码器统计信息获取
- ✅ 单流启动/停止
- ✅ 运行状态检查
- ✅ 多流并发测试

### 测试命令
```bash
cd test
python test_decoder_pool.py
```

## 迁移指南

### 对于用户
- **无需修改**: 所有现有API调用保持兼容
- **性能提升**: 自动获得多核性能优势
- **新功能**: 可通过 `/decoder_stats` 监控状态

### 对于开发者
旧代码:
```python
thread = threading.Thread(target=worker, args=(...))
thread.start()
```

新代码:
```python
decoder_pool = get_decoder_pool()
decoder_pool.start_decoder(client_id, stream_url, protocol, fps)
```

## 已知限制

1. **最大进程数**: 硬编码为16，需手动修改代码调整
2. **队列积压**: 如果AI推理慢，队列会积压并丢帧
3. **Windows限制**: multiprocessing在Windows上需要 `if __name__ == '__main__'` 保护
4. **内存占用**: 大队列会占用较多内存

## 未来改进方向

1. **动态进程池**: 根据负载自动调整进程数
2. **GPU解码**: 集成FFmpeg硬件加速
3. **分布式**: 跨机器的进程池
4. **智能丢帧**: 基于帧重要性的选择性保留
5. **监控Dashboard**: 可视化进程池状态

## 文档清单

- ✅ [DECODER_POOL_ARCHITECTURE.md](DECODER_POOL_ARCHITECTURE.md) - 完整架构文档
- ✅ [DECODER_POOL_QUICKSTART.md](DECODER_POOL_QUICKSTART.md) - 快速开始指南
- ✅ [test_decoder_pool.py](../test/test_decoder_pool.py) - 测试脚本
- ✅ 本文档 - 升级总结

## Git提交信息

```bash
git add app/services/decoder.py
git add app/routers/inspection.py
git add test/test_decoder_pool.py
git add docs/DECODER_POOL_*.md

git commit -m "feat: 升级为基于进程池的FFmpeg解码架构

- 新增 DecoderPool 进程池管理器，支持16核并行
- 新增 FrameDispatcher 帧分发器
- 重构 inspection.py 使用进程池
- 新增完整的测试和文档
- API保持向后兼容
- 性能提升：支持16个并发视频流

Breaking changes: 无
"
```

## 验证清单

升级后请验证以下功能：

- [ ] 后端正常启动
- [ ] 单个RTSP流可以正常启动
- [ ] AI推理正常接收帧
- [ ] 流可以正常停止
- [ ] 多个流可以并发运行
- [ ] `/decoder_stats` 端点返回正确信息
- [ ] 进程正常清理，无僵尸进程
- [ ] 内存使用正常，无泄漏

## 支持

如有问题，请查阅：
1. [快速开始](DECODER_POOL_QUICKSTART.md)
2. [架构文档](DECODER_POOL_ARCHITECTURE.md)
3. 运行测试脚本: `python test/test_decoder_pool.py`
4. 查看后端日志

---

**升级完成！** 🎉

现在系统可以充分利用16核CPU并行处理多个视频流了。
