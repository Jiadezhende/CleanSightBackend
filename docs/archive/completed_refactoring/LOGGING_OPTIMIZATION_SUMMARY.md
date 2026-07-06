# 启动日志优化总结

**日期**: 2026-02-20  
**目标**: 精简启动日志，制定统一的日志开发规范

---

## ✅ 完成的工作

### 1. 创建日志开发规范文档

📄 **文件**: [docs/LOGGING_GUIDELINES.md](docs/LOGGING_GUIDELINES.md)

详细定义了：
- 各日志级别（INFO/DEBUG/WARNING/ERROR/CRITICAL）的使用场景
- 统一的日志格式：`[ModuleName] message`
- Unicode符号使用规范（✓✗❌📊⚙️📌）
- 参数格式化规范（使用`%`而非f-string）
- 环境差异配置（开发/生产）
- 性能优化建议
- 完整的示例代码

### 2. 配置模块日志优化

**修改文件**:
- [app/services/client/config.py](app/services/client/config.py)
- [app/services/inference/config.py](app/services/inference/config.py)
- [app/services/persistence/config.py](app/services/persistence/config.py)
- [app/services/stream/config.py](app/services/stream/config.py)
- [app/services/health_monitor/config.py](app/services/health_monitor/config.py)

**优化内容**:
- ✅ 配置详情块（`===`分隔符）移至DEBUG级别
- ✅ INFO级别只显示简洁的配置加载成功信息
- ✅ 统一使用`[ModuleName] Config loaded: path`格式

**效果对比**:
```
# 修改前（INFO级别）:
========== Client配置 ==========
队列: rt=30, ca=2700, segment=300
帧处理: 640x480, inference_fps=20
状态: stage=LEAK, timeout=30s
📌 队列/fps参数来源: inference_config.yaml (global.*)
==================================

# 修改后（INFO级别）:
[InferenceConfig] Loaded | stages=2, fps=30.0/20, batch=4

# 详细信息在DEBUG级别可见
```

### 3. Worker启动日志简化

**修改文件**:
- [app/services/persistence/workers/hls_worker.py](app/services/persistence/workers/hls_worker.py)
- [app/services/persistence/workers/alarm_worker.py](app/services/persistence/workers/alarm_worker.py)
- [app/services/inference/workers/writeback.py](app/services/inference/workers/writeback.py)
- [app/services/inference/workers/visualization.py](app/services/inference/workers/visualization.py)
- [app/services/inference/workers/temporal.py](app/services/inference/workers/temporal.py)

**优化内容**:
- ✅ 单个Worker启动日志从INFO改为DEBUG级别
- ✅ Pool汇总日志保留INFO级别
- ✅ 所有print语句改为logger
- ✅ 统一日志格式为`[WorkerName-ID] message`

**效果对比**:
```
# 修改前（INFO级别）:
[HLSWorker-0] 已启动
[HLSWorker-1] 已启动
[TemporalWorker-0] 已启动
[TemporalWorker-1] 已启动
[VisualizationWorker-0] 已启动
...（共约10行）

# 修改后（INFO级别）:
[HLSWorkerPool] Started 2 workers
[TemporalWorkerPool] Started 2 workers
[VisualizationWorkerPool] Started 4 workers
...（共约3行）

# 单个Worker日志在DEBUG级别可见
```

### 4. 主应用启动日志统一

**修改文件**: [app/main.py](app/main.py)

**优化内容**:
- ✅ 将`print()`改为`logger.info()`
- ✅ 统一格式为`[CleanSight] message`
- ✅ 简化参数显示格式

**效果对比**:
```
# 修改前:
============================================================
CleanSight Backend 配置检查
============================================================
环境: 开发环境 (.env.dev)
数据库: 116.204.65.72:5432/aidkdb
严格模式: True
调试模式: False
============================================================

# 修改后:
[CleanSight] Starting backend...
============================================================
CleanSight Backend 配置检查
============================================================
[CleanSight] Environment: 开发环境 (.env.dev)
[CleanSight] Database: 116.204.65.72:5432/aidkdb
[CleanSight] Strict mode: True | Debug: False
============================================================
```

### 5. 模型加载日志优化

**修改文件**:
- [app/services/models/bubble/detector.py](app/services/models/bubble/detector.py)
- [app/services/models/bending/detector.py](app/services/models/bending/detector.py)

**优化内容**:
- ✅ 将`print()`改为`logger`
- ✅ 加载过程日志移至DEBUG级别
- ✅ 模型加载完成保留INFO级别，显示关键信息
- ✅ 统一格式为`[BubbleDetector] Model loaded: path | classes=N`

**效果对比**:
```
# 修改前（INFO级别）:
正在加载气泡检测模型: ./app/data/bubble-best.pt
模型加载成功，类别数量: 1
检测类别: {0: 'bubble'}
正在加载内镜弯折检测模型: ./app/data/bend-best.pt
模型加载成功，类别数量: 1
检测类别: {0: 'bending'}

# 修改后（INFO级别）:
[BubbleDetector] Model loaded: ./app/data/bubble-best.pt | classes=1
[BendingDetector] Model loaded: ./app/data/bend-best.pt | classes=1

# 详细信息在DEBUG级别可见
```

### 6. Router生命周期日志优化

**修改文件**:
- [app/routers/health.py](app/routers/health.py)
- [app/routers/ai.py](app/routers/ai.py)

**优化内容**:
- ✅ 统一格式为`[RouterName] message`
- ✅ 简化参数显示，详细信息移至DEBUG
- ✅ 统计信息精简为关键指标

**效果对比**:
```
# 修改前:
全局健康监控已启动 | check_interval=1.0s, heartbeat_timeout=5.0s, 
reconnect_interval=5.0s, max_reconnect_attempts=5, orphan_timeout=30.0s

# 修改后（INFO级别）:
[GlobalHealthMonitor] Started | interval=1.0s, timeout=5.0s

# 详细参数在DEBUG级别可见
```

### 7. InferenceManager日志优化

**修改文件**:
- [app/services/inference/core/manager.py](app/services/inference/core/manager.py)
- [app/services/inference/core/dispatcher.py](app/services/inference/core/dispatcher.py)
- [app/services/client/manager.py](app/services/client/manager.py)

**优化内容**:
- ✅ 所有print语句改为logger
- ✅ 初始化详情移至DEBUG级别
- ✅ 关键启动信息保留INFO级别
- ✅ 统一格式为`[InferenceManager] message`

---

## 📊 优化效果

### 启动日志对比

#### ⚠️ 修改前（约60行）
```
18:13:27 INFO     [uvicorn.error] Will watch for changes in these directories...
18:13:27 INFO     [uvicorn.error] Uvicorn running on http://0.0.0.0:8000
18:13:27 INFO     [app.services.client.config] ✓ 已加载client配置: config/client_config.yaml
18:13:27 INFO     [app.services.inference.config] ✓ 已加载inference配置: config/inference_config.yaml
18:13:27 INFO     [app.services.inference.config] ========== Inference配置 ==========
18:13:27 INFO     [app.services.inference.config] Stage数量: 2
18:13:27 INFO     [app.services.inference.config] FPS配置: raw_fps=30.0, inference_fps=20
18:13:27 INFO     [app.services.inference.config] 队列配置: rt_maxlen=30, ca_maxlen=2700
18:13:27 INFO     [app.services.inference.config] 批处理: batch_size=4, decimation=2
18:13:27 INFO     [app.services.inference.config] 📌 此文件为所有模块共享参数的单一数据源
18:13:27 INFO     [app.services.inference.config] =====================================
18:13:27 INFO     [app.services.client.config] ========== Client配置 ==========
18:13:27 INFO     [app.services.client.config] 队列: rt=30, ca=2700, segment=300
18:13:27 INFO     [app.services.client.config] 帧处理: 640x480, inference_fps=20
18:13:27 INFO     [app.services.client.config] 状态: stage=LEAK, timeout=30s
18:13:27 INFO     [app.services.client.config] 📌 队列/fps参数来源: inference_config.yaml
18:13:27 INFO     [app.services.client.config] ==================================
... （还有40多行）
```

#### ✅ 修改后（约20行）
```
18:13:27 INFO     [uvicorn.error] Uvicorn running on http://0.0.0.0:8000
18:13:27 INFO     [CleanSight] Starting backend...
18:13:27 INFO     [CleanSight] Environment: 开发环境 (.env.dev)
18:13:27 INFO     [CleanSight] Database: 116.204.65.72:5432/aidkdb
18:13:27 INFO     [CleanSight] Strict mode: True | Debug: False
18:13:27 INFO     [ClientManager] Initialized
18:13:27 INFO     [InferenceConfig] Loaded | stages=2, fps=30.0/20, batch=4
18:13:29 INFO     [BubbleDetector] Model loaded: ./app/data/bubble-best.pt | classes=1
18:13:29 INFO     [BendingDetector] Model loaded: ./app/data/bend-best.pt | classes=1
18:13:29 INFO     [InferenceService] Warmup completed | stages=2, elapsed=705.5ms
18:13:29 INFO     [HLSWorkerPool] Started 2 workers
18:13:29 INFO     [AlarmWorkerPool] Started 1 worker + batch thread
18:13:29 INFO     [TemporalWorkerPool] Started 2 workers
18:13:29 INFO     [VisualizationWorkerPool] Started 4 workers
18:13:29 INFO     [WriteBackWorkerPool] Started 2 workers
18:13:29 INFO     [GlobalHealthMonitor] Started | interval=1.0s, timeout=5.0s
18:13:29 INFO     [InferenceManager] Started
18:13:29 INFO     [AIRouter] Inference service started
18:13:29 INFO     [uvicorn.error] Application startup complete.
```

**减少约67%的启动日志行数！** 🎉

---

## 🔍 查看详细日志

如果需要查看配置详情和Worker启动信息，设置日志级别为DEBUG：

```powershell
# Windows PowerShell
$env:LOG_LEVEL="DEBUG"; ./start_backend.ps1 dev

# Linux/Mac
LOG_LEVEL=DEBUG ./start_backend.sh dev
```

DEBUG级别会显示：
- 所有配置详情块（队列大小、FPS参数等）
- 单个Worker的启动/停止日志
- 模型加载过程详情（类别名称等）
- InferenceManager内部状态变化
- Dispatcher调度详情

---

## 📝 日志格式规范

### 统一格式
```python
logger.info("[ModuleName] message with %s", param)
```

### 模块命名规范
- **服务/管理器**: `[ClientManager]`, `[InferenceService]`
- **Worker**: `[HLSWorker-0]`, `[TemporalWorker-1]`
- **Worker Pool**: `[HLSWorkerPool]`, `[TemporalWorkerPool]`
- **Router**: `[AIRouter]`, `[GlobalHealthMonitor]`
- **模型**: `[BubbleDetector]`, `[BendingDetector]`
- **配置**: `[ClientConfig]`, `[InferenceConfig]`
- **主应用**: `[CleanSight]`

### 参数分隔符
- 多个独立参数: `|` 分隔
  ```python
  logger.info("[Service] Started | models=%d, workers=%d", models, workers)
  ```
- 列表项: `,` 分隔
  ```python
  logger.info("[Service] Config: width=%d, height=%d, fps=%d", w, h, fps)
  ```

### Unicode符号使用
- `✓` - 操作成功
- `✗` - 操作失败但可恢复
- `❌` - 致命错误
- `📊` - 统计信息
- `⚙️` - 配置信息
- `📌` - 重要提示

---

## 🎯 关键改进点

1. **启动时日志精简**：从约60行减少到约20行（减少67%）
2. **格式统一**：所有日志使用`[ModuleName] message`格式
3. **级别合理**：
   - INFO: 服务启动、配置加载成功、模型加载完成
   - DEBUG: 配置详情、Worker启动、内部状态
4. **参数格式化**：统一使用`%`而非f-string（性能优化）
5. **消除print**：所有print语句改为logger（便于日志管理）

---

## ✅ 验证步骤

1. **查看INFO级别日志**（默认）：
   ```powershell
   ./start_backend.ps1 dev
   ```
   验证日志简洁清晰，关键信息齐全

2. **查看DEBUG级别日志**：
   ```powershell
   $env:LOG_LEVEL="DEBUG"; ./start_backend.ps1 dev
   ```
   验证详细信息正确显示

3. **检查日志格式**：
   - 确认所有日志都有`[ModuleName]`前缀
   - 确认参数使用`%`格式化
   - 确认没有print语句输出

---

## 📚 相关文档

- [日志开发规范](docs/LOGGING_GUIDELINES.md) - 完整的日志使用指南
- [架构文档](kb/ARCHITECTURE_OVERVIEW.md) - 系统架构说明
- [配置指南](docs/CONFIGURATION_GUIDE.md) - 配置文件说明

---

**维护者**: CleanSight Backend Team  
**版本**: 1.0  
**最后更新**: 2026-02-20
