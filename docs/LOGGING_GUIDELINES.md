# 日志开发规范

本文档定义CleanSight Backend项目的日志使用标准和最佳实践。

---

## 1. 日志级别使用准则

### 1.1 INFO 级别

**用途**：记录应用程序的正常运行状态和关键里程碑事件。

**使用场景**：
- ✅ **服务启动/停止**：`[AIRouter] Inference service started`
- ✅ **配置加载成功**：`[ClientConfig] Config loaded: config/client_config.yaml`
- ✅ **模型加载完成**：`[BubbleDetection] Model loaded: ./app/data/bubble-best.pt`
- ✅ **模型预热统计**：`[InferenceService] Warmup completed | stages=2, elapsed=705.5ms`
- ✅ **关键业务操作**：`[TaskManager] Task created: id=1234567890`
- ✅ **资源池汇总信息**：`[HLSWorkerPool] Started 2 workers`
- ✅ **健康监控统计**：`[GlobalHealthMonitor] Stopped | checks=1000, cleanups=5`

**示例**：
```python
logger.info("[ClientManager] Initialized")
logger.info("[InferenceService] Started | models=2, workers=1, CUDA=enabled")
logger.info("[StreamDecoder] Connected to %s | resolution=%dx%d", rtsp_url, width, height)
```

### 1.2 DEBUG 级别

**用途**：记录详细的内部执行流程，用于开发调试和问题排查。

**使用场景**：
- ✅ **配置详情**：队列大小、FPS参数、超时时间等详细配置
- ✅ **单个Worker启动/停止**：`[HLSWorker-0] Started`
- ✅ **内部状态变化**：队列长度、处理进度、资源使用情况
- ✅ **详细执行流程**：函数调用、条件分支、循环迭代
- ✅ **模型加载过程**："正在加载模型..."、"检测类别: {0: 'bubble'}"
- ✅ **数据处理细节**：帧解码、推理结果、告警判断逻辑

**示例**：
```python
logger.debug("[HLSWorker-0] Started")
logger.debug("[InferenceService] Processing batch: size=%d, stage=%s", batch_size, stage)
logger.debug("[ClientManager] Current clients: %d active, %d pending", active, pending)
```

### 1.3 WARNING 级别

**用途**：记录可恢复的异常情况或潜在问题。

**使用场景**：
- ✅ **配置文件缺失，使用默认值**：`[ClientConfig] Config file not found, using defaults`
- ✅ **资源即将耗尽**：`[StreamDecoder] Queue 90% full, backpressure activated`
- ✅ **可恢复的连接失败**：`[StreamDecoder] Connection failed, will retry in 5s`
- ✅ **配置参数超出推荐范围**：`[InferenceConfig] batch_size=16 exceeds recommended value`
- ✅ **降级运行**：`[InferenceService] CUDA unavailable, using CPU mode`

**示例**：
```python
logger.warning("[ClientConfig] ✗ Config file not found: %s, using defaults", config_path)
logger.warning("[StreamDecoder] Frame drop detected: dropped=%d, total=%d", dropped, total)
```

### 1.4 ERROR 级别

**用途**：记录导致功能失败的错误，需要人工介入。

**使用场景**：
- ✅ **操作失败**：任务创建失败、文件写入失败
- ✅ **连接断开**：RTSP连接中断、数据库连接丢失
- ✅ **数据损坏**：帧解码失败、模型推理异常
- ✅ **资源耗尽**：内存不足、磁盘空间满
- ✅ **配置错误**：必需参数缺失、参数类型错误

**示例**：
```python
logger.error("[StreamDecoder] ❌ Failed to connect to RTSP: %s", rtsp_url, exc_info=True)
logger.error("[PersistenceService] ❌ Database write failed: %s", error, exc_info=True)
```

### 1.5 CRITICAL 级别

**用途**：记录导致系统无法继续运行的严重错误。

**使用场景**：
- ✅ **服务初始化失败**：必要组件无法启动
- ✅ **致命资源错误**：无法访问数据库、模型文件丢失
- ✅ **系统崩溃前兆**：内存泄漏、死锁检测

**示例**：
```python
logger.critical("[InferenceService] ❌ Model file not found: %s, cannot start", model_path)
logger.critical("[Database] ❌ Connection pool exhausted, system unstable")
```

---

## 2. 日志格式规范

### 2.1 统一格式：`[ModuleName] message`

**所有日志必须使用方括号前缀标识模块名称**，便于快速定位日志来源。

**规则**：
- 方括号内使用**PascalCase**（大驼峰）命名：`[ClientManager]`、`[InferenceService]`
- 对于Worker，使用**名称-编号**格式：`[HLSWorker-0]`、`[TemporalWorker-1]`
- 对于Pool/Manager，使用**Pool/Manager后缀**：`[HLSWorkerPool]`、`[ClientManager]`
- 消息内容使用简洁的**英文动词开头**或**中文描述**

**示例**：
```python
# ✓ 正确
logger.info("[ClientManager] Initialized")
logger.info("[InferenceService] Started | models=2, workers=1")
logger.info("[HLSWorker-0] Processing segment: id=%s", segment_id)

# ✗ 错误
logger.info("ClientManager已初始化")  # 缺少方括号
logger.info("[client_manager] Initialized")  # 应使用PascalCase
logger.info("Initialized")  # 无法识别模块来源
```

### 2.2 参数格式化

**使用`%`格式化而非f-string**，以提高日志系统性能（延迟计算）。

```python
# ✓ 正确
logger.info("[StreamDecoder] Connected to %s | resolution=%dx%d", rtsp_url, width, height)
logger.debug("[InferenceService] Batch size: %d, elapsed: %.2fms", batch_size, elapsed)

# ✗ 错误（会提前计算字符串）
logger.info(f"[StreamDecoder] Connected to {rtsp_url} | resolution={width}x{height}")
```

### 2.3 分隔符使用

#### 2.3.1 参数分隔：使用 `|` 或 `,`

```python
# 多个独立参数用 | 分隔
logger.info("[InferenceService] Started | models=2, workers=1, CUDA=enabled")

# 列表项用 , 分隔
logger.info("[TaskManager] Task updated | id=%s, status=%s, client=%s", task_id, status, client_id)
```

#### 2.3.2 配置块分隔：调试时使用 `===`

配置详情块仅在DEBUG级别显示，使用等号分隔：

```python
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("========== ClientConfig ==========")
    logger.debug("Queues: rt=%d, ca=%d, segment=%d", rt, ca, segment)
    logger.debug("Frame: %dx%d, inference_fps=%d", width, height, fps)
    logger.debug("==================================")
```

### 2.4 Unicode符号使用

**用于增强可读性，但需谨慎使用**。

| 符号 | 含义 | 使用场景 |
|------|------|----------|
| ✓ | 成功 | 配置加载成功、操作完成 |
| ✗ | 失败（可恢复） | 配置文件缺失、连接失败但会重试 |
| ❌ | 失败（严重） | 致命错误、无法恢复的异常 |
| 📊 | 统计信息 | 性能指标、运行统计 |
| ⚙️ | 配置信息 | 参数设置、环境配置 |
| 📌 | 重要提示 | 注意事项、配置来源说明 |

**示例**：
```python
logger.info("[ClientConfig] ✓ Config loaded: %s", config_path)
logger.warning("[ClientConfig] ✗ Config file not found, using defaults")
logger.error("[StreamDecoder] ❌ Connection failed: %s", error, exc_info=True)
logger.info("[InferenceService] 📊 Warmup completed | elapsed=%.1fms", elapsed)
```

---

## 3. 模块日志规范

### 3.1 配置加载日志

**INFO级别**：简洁的加载成功/失败信息
```python
# 成功
logger.info("[ClientConfig] ✓ Config loaded: %s", config_path)

# 使用默认值（WARNING）
logger.warning("[ClientConfig] ✗ Config file not found: %s, using defaults", config_path)

# 加载失败（ERROR）
logger.error("[ClientConfig] ❌ Failed to load config: %s", error, exc_info=True)
```

**DEBUG级别**：详细的配置内容
```python
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("========== ClientConfig ==========")
    logger.debug("Queues: rt=%d, ca=%d", rt_queue, ca_queue)
    logger.debug("FPS: raw=%.1f, inference=%d", raw_fps, inference_fps)
    logger.debug("==================================")
```

### 3.2 服务启动/停止日志

**启动**：
```python
# 简单服务
logger.info("[ClientManager] Initialized")

# 复杂服务（带关键参数）
logger.info("[InferenceService] Started | models=%d, workers=%d, CUDA=%s", 
            model_count, worker_count, "enabled" if cuda else "disabled")
```

**停止**：
```python
# 简单停止
logger.info("[ClientManager] Stopped")

# 带统计信息
logger.info("[GlobalHealthMonitor] Stopped | checks=%d, cleanups=%d, reconnects=%d", 
            total_checks, total_cleanups, total_reconnects)
```

### 3.3 Worker日志

**单个Worker**（DEBUG级别）：
```python
logger.debug("[HLSWorker-%d] Started", worker_id)
logger.debug("[HLSWorker-%d] Processing: segment=%s, client=%s", worker_id, segment_id, client_id)
logger.debug("[HLSWorker-%d] Stopped", worker_id)
```

**Worker Pool**（INFO级别）：
```python
logger.info("[HLSWorkerPool] Started %d workers", num_workers)
logger.info("[HLSWorkerPool] Stopped | processed=%d, failed=%d", total_processed, total_failed)
```

### 3.4 模型加载日志

**加载过程**（DEBUG级别）：
```python
logger.debug("[BubbleDetection] Loading model: %s", model_path)
logger.debug("[BubbleDetection] Model classes: %s", class_names)
```

**加载完成**（INFO级别）：
```python
logger.info("[BubbleDetection] Model loaded: %s | classes=%d", model_path, num_classes)
```

**预热统计**（INFO级别）：
```python
logger.info("[InferenceService] Warmup completed | stages=%d, elapsed=%.1fms", num_stages, elapsed)
```

### 3.5 业务操作日志

**任务操作**：
```python
logger.info("[TaskManager] Task created | id=%s, stage=%s, client=%s", task_id, stage, client_id)
logger.info("[TaskManager] Task updated | id=%s, status=%s", task_id, status)
logger.info("[TaskManager] Task deleted | id=%s", task_id)
```

**连接操作**：
```python
logger.info("[StreamDecoder] Connected to %s | resolution=%dx%d@%dfps", rtsp_url, width, height, fps)
logger.warning("[StreamDecoder] Connection lost, reconnecting in %ds", retry_delay)
logger.info("[StreamDecoder] Disconnected | frames_received=%d, dropped=%d", total_frames, dropped_frames)
```

---

## 4. 环境差异

### 4.1 开发环境（.env.dev）

**默认配置**：
- 日志级别：`INFO`
- 日志格式：彩色输出（使用colorlog）
- 输出目标：控制台
- 时间格式：`HH:MM:SS`

**启动方式**：
```powershell
./start_backend.ps1 dev
```

**查看调试日志**：
```powershell
$env:LOG_LEVEL="DEBUG"; ./start_backend.ps1 dev
```

### 4.2 生产环境（.env）

**默认配置**：
- 日志级别：`WARNING`
- 日志格式：纯文本（无颜色）
- 输出目标：控制台 + 文件
- 时间格式：`YYYY-MM-DD HH:MM:SS`

**启动方式**：
```bash
./start_backend.sh prod
```

**临时启用INFO日志**：
```bash
LOG_LEVEL=INFO ./start_backend.sh prod
```

---

## 5. 常见反面模式（避免）

### 5.1 ❌ 使用print()代替logging

```python
# ✗ 错误
print("ClientManager已初始化")

# ✓ 正确
logger.info("[ClientManager] Initialized")
```

### 5.2 ❌ 日志级别使用不当

```python
# ✗ 错误：将调试信息记录为INFO
logger.info("[StreamDecoder] Frame %d decoded, size=%d bytes", frame_id, size)

# ✓ 正确：详细的处理信息应使用DEBUG
logger.debug("[StreamDecoder] Frame %d decoded, size=%d bytes", frame_id, size)
```

### 5.3 ❌ 缺少模块前缀

```python
# ✗ 错误
logger.info("Inference service started")

# ✓ 正确
logger.info("[InferenceService] Started")
```

### 5.4 ❌ 过度使用f-string格式化

```python
# ✗ 错误（提前计算字符串）
logger.debug(f"[Worker] Processing {task_id} with {len(frames)} frames")

# ✓ 正确（延迟计算）
logger.debug("[Worker] Processing %s with %d frames", task_id, len(frames))
```

### 5.5 ❌ INFO级别记录过多细节

```python
# ✗ 错误：启动时打印过多配置详情
logger.info("========== ClientConfig ==========")
logger.info("Queue sizes: rt=%d, ca=%d", rt, ca)
logger.info("Frame size: %dx%d", width, height)
logger.info("==================================")

# ✓ 正确：启动时只显示汇总信息
logger.info("[ClientConfig] ✓ Config loaded: %s", config_path)

# DEBUG级别显示详情
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("========== ClientConfig ==========")
    logger.debug("Queue sizes: rt=%d, ca=%d", rt, ca)
    logger.debug("Frame size: %dx%d", width, height)
    logger.debug("==================================")
```

---

## 6. 性能考虑

### 6.1 避免在热路径记录DEBUG日志

**热路径**：每秒执行数千次的代码（如帧处理循环）

```python
# ✗ 错误：会严重影响性能
for frame in frames:
    logger.debug("[InferenceService] Processing frame %d", frame.id)
    process_frame(frame)

# ✓ 正确：使用批量日志或采样日志
processed_count = 0
for frame in frames:
    process_frame(frame)
    processed_count += 1

logger.debug("[InferenceService] Batch processed: %d frames", processed_count)
```

### 6.2 使用条件日志

对于需要复杂计算的日志，先检查日志级别：

```python
# ✗ 低效：即使DEBUG未启用也会计算
logger.debug("[Stats] Frame stats: %s", calculate_expensive_stats(frames))

# ✓ 高效：仅在DEBUG启用时计算
if logger.isEnabledFor(logging.DEBUG):
    logger.debug("[Stats] Frame stats: %s", calculate_expensive_stats(frames))
```

---

## 7. 示例：完整的模块日志

### 示例：ClientManager

```python
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class ClientManager:
    def __init__(self, config):
        self.config = config
        self.clients = {}
        
        # INFO: 初始化完成
        logger.info("[ClientManager] Initialized")
        
        # DEBUG: 配置详情
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("========== ClientManager Config ==========")
            logger.debug("Max clients: %d", config.max_clients)
            logger.debug("Timeout: %ds", config.timeout)
            logger.debug("==========================================")
    
    def add_client(self, client_id: str, rtsp_url: str) -> bool:
        try:
            # DEBUG: 操作细节
            logger.debug("[ClientManager] Adding client: id=%s, url=%s", client_id, rtsp_url)
            
            # ... 业务逻辑 ...
            
            # INFO: 成功创建
            logger.info("[ClientManager] Client added | id=%s, total=%d", client_id, len(self.clients))
            return True
            
        except Exception as e:
            # ERROR: 操作失败
            logger.error("[ClientManager] ❌ Failed to add client: %s", client_id, exc_info=True)
            return False
    
    def remove_client(self, client_id: str) -> bool:
        if client_id not in self.clients:
            # WARNING: 客户端不存在
            logger.warning("[ClientManager] Client not found: %s", client_id)
            return False
        
        # DEBUG: 操作细节
        logger.debug("[ClientManager] Removing client: %s", client_id)
        
        del self.clients[client_id]
        
        # INFO: 成功移除
        logger.info("[ClientManager] Client removed | id=%s, remaining=%d", client_id, len(self.clients))
        return True
    
    def shutdown(self):
        client_count = len(self.clients)
        
        # INFO: 关闭统计
        logger.info("[ClientManager] Shutting down | active_clients=%d", client_count)
        
        # DEBUG: 清理细节
        for client_id in list(self.clients.keys()):
            logger.debug("[ClientManager] Cleaning up client: %s", client_id)
            self.remove_client(client_id)
        
        # INFO: 关闭完成
        logger.info("[ClientManager] Stopped")
```

---

## 8. 检查清单

在提交代码前，请确认：

- [ ] 所有日志使用`[ModuleName]`前缀
- [ ] 使用`%`格式化而非f-string
- [ ] INFO日志只包含关键信息（服务启动、配置加载、重要操作）
- [ ] 详细信息（配置详情、Worker启动）使用DEBUG级别
- [ ] WARNING用于可恢复的异常，ERROR用于需要介入的错误
- [ ] 异常日志包含`exc_info=True`参数
- [ ] 没有使用`print()`替代`logger`
- [ ] 热路径（高频循环）避免记录DEBUG日志
- [ ] Unicode符号（✓✗❌）使用恰当

---

## 9. 参考资料

- [Python Logging HOWTO](https://docs.python.org/3/howto/logging.html)
- [Colorlog Documentation](https://github.com/borntyping/python-colorlog)
- 项目配置文件：[logging_config.json](../logging_config.json)
- 日志配置备用：[logging_config_fallback.json](../logging_config_fallback.json)

---

**文档版本**：1.0  
**最后更新**：2026-02-20  
**维护者**：CleanSight Backend Team
