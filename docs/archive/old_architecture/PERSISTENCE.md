# Persistence模块开发指南

## 📋 目录

1. [模块架构](#模块架构)
2. [快速开始：添加新策略](#快速开始添加新策略)
3. [详细步骤](#详细步骤)
4. [完整示例：添加指标持久化](#完整示例添加指标持久化)
5. [最佳实践](#最佳实践)
6. [测试指南](#测试指南)
7. [故障排查](#故障排查)

---

## 模块架构

### 核心设计思想

Persistence模块基于**策略模式 + Worker池模式**，实现持久化任务的异步处理：

```
┌──────────────────────────────────────────────────────────┐
│                   PersistenceManager                      │
│  职责：协调各个持久化策略，提供统一的API接口              │
└──────────┬──────────────────────┬──────────────────┬─────┘
           │                      │                  │
           ↓                      ↓                  ↓
    ┌──────────────┐      ┌──────────────┐   ┌──────────────┐
    │  HLS队列     │      │  告警队列     │   │  新策略队列   │
    │ (256容量)    │      │ (128容量)    │   │  (自定义)    │
    └──────┬───────┘      └──────┬───────┘   └──────┬───────┘
           │                      │                  │
           ↓                      ↓                  ↓
    ┌──────────────┐      ┌──────────────┐   ┌──────────────┐
    │HLSWorkerPool │      │AlarmWorkerPool│   │ 新WorkerPool │
    │  (2线程)     │      │ (1线程+flush)│   │  (自定义)    │
    └──────┬───────┘      └──────┬───────┘   └──────┬───────┘
           │                      │                  │
           ↓                      ↓                  ↓
    ┌──────────────┐      ┌──────────────┐   ┌──────────────┐
    │ HLSStrategy  │      │AlarmStrategy │   │ 新Strategy   │
    │ - MP4编码    │      │ - HTTP上报   │   │ - 业务逻辑   │
    │ - M3U8生成   │      │ - 数据库记录 │   │ - 自定义操作 │
    └──────────────┘      └──────────────┘   └──────────────┘
```

### 模块文件结构

```
app/services/persistence/
├── __init__.py                 # 模块导出
├── manager.py                  # PersistenceManager（协调器）
├── models.py                   # 数据模型定义
├── config.py                   # 配置模型
├── strategies/                 # 策略实现
│   ├── __init__.py
│   ├── hls_strategy.py        # HLS持久化策略
│   └── alarm_strategy.py      # 告警持久化策略
└── workers/                    # Worker池实现
    ├── __init__.py
    ├── hls_worker.py          # HLS Worker池
    └── alarm_worker.py        # 告警Worker池
```

### 核心概念

| 概念 | 职责 | 示例 |
|------|------|------|
| **Task Model** | 定义持久化任务的数据结构 | `HLSPersistenceTask`, `AlarmPersistenceTask` |
| **Strategy** | 实现具体的持久化逻辑 | 编码视频、发送HTTP、写数据库 |
| **Worker** | 从队列取任务，调用Strategy执行 | 后台线程池 |
| **WorkerPool** | 管理多个Worker线程 | 启动、停止、监控 |
| **Manager** | 统一入口，协调所有策略 | 路由任务到正确的队列 |

---

## 快速开始：添加新策略

### 5步添加新策略

假设要添加**指标持久化策略**（将推理性能指标存入时序数据库）：

```bash
# 步骤概览
1. 定义任务模型（models.py）
2. 实现策略类（strategies/metrics_strategy.py）
3. 实现Worker池（workers/metrics_worker.py）
4. 在Manager中集成（manager.py）
5. 更新配置（config.py）
```

---

## 详细步骤

### 步骤1: 定义任务模型

**文件**: `app/services/persistence/models.py`

```python
from dataclasses import dataclass, field
from typing import Dict, Any, List
import time

@dataclass
class MetricsPersistenceTask:
    """指标持久化任务

    Attributes:
        client_id: 客户端ID
        task_id: 任务ID
        metrics: 指标数据列表，每个元素为 {metric_name, value, timestamp}
        timestamp: 任务创建时间
    """
    client_id: str
    task_id: int
    metrics: List[Dict[str, Any]]
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        """验证数据完整性"""
        if not self.metrics:
            raise ValueError("metrics不能为空")

        for metric in self.metrics:
            if 'name' not in metric or 'value' not in metric:
                raise ValueError("每个metric必须包含name和value字段")
```

**关键点**：
- 使用`@dataclass`简化代码
- `timestamp`默认使用当前时间
- 添加`__post_init__`做参数验证
- 添加清晰的docstring

---

### 步骤2: 实现策略类

**文件**: `app/services/persistence/strategies/metrics_strategy.py`

```python
"""
指标持久化策略

负责：
- 将性能指标批量写入时序数据库
- 数据格式转换
- 错误重试
"""

from typing import List, Dict, Any
import logging
import time

logger = logging.getLogger(__name__)


class MetricsPersistenceStrategy:
    """指标持久化策略"""

    def __init__(
        self,
        db_connection_string: str,
        batch_size: int = 100,
        retry_times: int = 3
    ):
        """初始化策略

        Args:
            db_connection_string: 数据库连接字符串
            batch_size: 批量写入大小
            retry_times: 失败重试次数
        """
        self.db_conn = db_connection_string
        self.batch_size = batch_size
        self.retry_times = retry_times

        # 初始化数据库连接（示例）
        # self.client = InfluxDBClient(...)

        logger.info("MetricsStrategy已初始化: batch_size=%d", batch_size)

    def persist_metrics(
        self,
        client_id: str,
        task_id: int,
        metrics: List[Dict[str, Any]]
    ) -> bool:
        """持久化指标数据

        Args:
            client_id: 客户端ID
            task_id: 任务ID
            metrics: 指标列表

        Returns:
            bool: 成功返回True，失败返回False
        """
        if not metrics:
            logger.warning("指标列表为空，跳过持久化")
            return False

        try:
            # 1. 转换为数据库格式
            points = self._convert_to_points(client_id, task_id, metrics)

            # 2. 批量写入（带重试）
            success = self._write_with_retry(points)

            if success:
                logger.info(
                    "指标已持久化: client=%s, task=%d, count=%d",
                    client_id, task_id, len(metrics)
                )

            return success

        except Exception as e:
            logger.error(
                "指标持久化失败: client=%s, error=%s",
                client_id, e, exc_info=True
            )
            return False

    def _convert_to_points(
        self,
        client_id: str,
        task_id: int,
        metrics: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """转换为时序数据点格式

        示例输入:
            [{'name': 'fps', 'value': 30.5, 'timestamp': 1234567890}]

        示例输出:
            [{
                'measurement': 'inference_metrics',
                'tags': {'client_id': 'xxx', 'task_id': 123},
                'fields': {'fps': 30.5},
                'time': 1234567890
            }]
        """
        points = []
        for metric in metrics:
            point = {
                'measurement': 'inference_metrics',
                'tags': {
                    'client_id': client_id,
                    'task_id': str(task_id),
                },
                'fields': {
                    metric['name']: metric['value']
                },
                'time': metric.get('timestamp', time.time())
            }
            points.append(point)

        return points

    def _write_with_retry(self, points: List[Dict]) -> bool:
        """带重试的写入"""
        backoff = 1.0

        for attempt in range(1, self.retry_times + 1):
            try:
                # 实际写入逻辑（示例）
                # self.client.write_points(points)

                logger.debug("指标写入成功，尝试次数=%d", attempt)
                return True

            except Exception as e:
                logger.warning(
                    "指标写入失败（尝试 %d/%d）: %s",
                    attempt, self.retry_times, e
                )

                if attempt < self.retry_times:
                    time.sleep(backoff)
                    backoff *= 2
                else:
                    return False

        return False
```

**关键点**：
- 构造函数初始化数据库连接
- 使用`logging`记录日志，不用`print`
- 实现重试逻辑
- 清晰的方法文档
- 异常捕获和错误处理

---

### 步骤3: 实现Worker池

**文件**: `app/services/persistence/workers/metrics_worker.py`

```python
"""
指标持久化Worker池

负责：
- 从队列消费指标任务
- 调用策略执行持久化
- 并发处理
"""

import threading
import logging
from queue import Empty, Queue
from typing import Optional

from app.services.persistence.models import MetricsPersistenceTask
from app.services.persistence.strategies.metrics_strategy import MetricsPersistenceStrategy

logger = logging.getLogger(__name__)


class MetricsWorker(threading.Thread):
    """指标持久化Worker"""

    def __init__(
        self,
        input_queue: Queue,
        strategy: MetricsPersistenceStrategy,
        stop_event: threading.Event,
        worker_id: int
    ):
        super().__init__(daemon=True)
        self.input_queue = input_queue
        self.strategy = strategy
        self.stop_event = stop_event
        self.worker_id = worker_id

    def run(self):
        """工作循环"""
        logger.info("MetricsWorker-%d 已启动", self.worker_id)

        while not self.stop_event.is_set():
            try:
                # 从队列获取任务
                try:
                    task: MetricsPersistenceTask = self.input_queue.get(timeout=0.5)
                except Empty:
                    continue

                # 执行持久化
                try:
                    self.strategy.persist_metrics(
                        client_id=task.client_id,
                        task_id=task.task_id,
                        metrics=task.metrics
                    )
                except Exception as e:
                    logger.error(
                        "MetricsWorker-%d 持久化失败: %s",
                        self.worker_id, e, exc_info=True
                    )

            except Exception as e:
                logger.error(
                    "MetricsWorker-%d 异常: %s",
                    self.worker_id, e, exc_info=True
                )

        logger.info("MetricsWorker-%d 已停止", self.worker_id)


class MetricsWorkerPool:
    """指标持久化Worker池"""

    def __init__(
        self,
        input_queue: Queue,
        strategy: MetricsPersistenceStrategy,
        num_workers: int = 2
    ):
        """初始化Worker池

        Args:
            input_queue: 输入队列
            strategy: 持久化策略
            num_workers: Worker线程数
        """
        self.input_queue = input_queue
        self.strategy = strategy
        self.num_workers = num_workers

        self.workers = []
        self.threads = []
        self.stop_event = threading.Event()

    def start(self):
        """启动Worker池"""
        logger.info("启动 %d 个指标Worker", self.num_workers)

        for i in range(self.num_workers):
            worker = MetricsWorker(
                input_queue=self.input_queue,
                strategy=self.strategy,
                stop_event=self.stop_event,
                worker_id=i
            )
            thread = threading.Thread(target=worker.run, daemon=True)

            self.workers.append(worker)
            self.threads.append(thread)
            thread.start()

    def stop(self, timeout: float = 10.0):
        """停止Worker池

        Args:
            timeout: 等待超时时间（秒）
        """
        logger.info("停止指标Worker池")
        self.stop_event.set()

        for thread in self.threads:
            thread.join(timeout=timeout)
```

**关键点**：
- Worker继承`threading.Thread`
- 使用`stop_event`优雅停止
- `timeout=0.5`避免线程卡死
- 捕获所有异常，防止线程崩溃
- Pool管理多个Worker的生命周期

---

### 步骤4: 在Manager中集成

**文件**: `app/services/persistence/manager.py`

```python
# 添加导入
from app.services.persistence.models import MetricsPersistenceTask
from app.services.persistence.strategies.metrics_strategy import MetricsPersistenceStrategy
from app.services.persistence.workers.metrics_worker import MetricsWorkerPool

class PersistenceManager:
    def __init__(self, config: PersistenceConfig):
        # ... 现有代码 ...

        # 新增：指标持久化
        self.metrics_queue: "queue.Queue[MetricsPersistenceTask]" = queue.Queue(
            maxsize=512
        )

        metrics_strategy = MetricsPersistenceStrategy(
            db_connection_string=config.metrics_db_url,
            batch_size=config.metrics_batch_size,
            retry_times=3
        )

        self.metrics_pool = MetricsWorkerPool(
            input_queue=self.metrics_queue,
            strategy=metrics_strategy,
            num_workers=config.metrics_workers
        )

    def persist_metrics(
        self,
        client_id: str,
        task_id: int,
        metrics: List[Dict[str, Any]]
    ) -> bool:
        """持久化性能指标（新增API）

        Args:
            client_id: 客户端ID
            task_id: 任务ID
            metrics: 指标列表，格式 [{'name': 'fps', 'value': 30.5}, ...]

        Returns:
            bool: 成功返回True，失败返回False

        Example:
            >>> manager.persist_metrics(
            ...     client_id="client_1",
            ...     task_id=123,
            ...     metrics=[
            ...         {'name': 'fps', 'value': 30.5},
            ...         {'name': 'latency_ms', 'value': 15.2}
            ...     ]
            ... )
            True
        """
        task = MetricsPersistenceTask(
            client_id=client_id,
            task_id=task_id,
            metrics=metrics
        )

        try:
            self.metrics_queue.put_nowait(task)
            self.metrics.metrics_submitted += 1
            return True
        except queue.Full:
            self.metrics.metrics_queue_full += 1
            logger.warning("指标队列已满，丢弃任务: %s", client_id)
            return False
        except Exception as e:
            self.metrics.metrics_errors += 1
            logger.error("指标入队失败: %s", e, exc_info=True)
            return False

    def start(self):
        """启动持久化服务"""
        logger.info("启动持久化服务")
        self.hls_pool.start()
        self.alarm_pool.start()
        self.metrics_pool.start()  # 新增

    def stop(self, timeout: float = 10.0):
        """停止持久化服务（优雅关闭）"""
        logger.info("停止持久化服务")
        self._stop_event.set()

        # 停止Worker池（会等待队列清空）
        self.hls_pool.stop(timeout=timeout)
        self.alarm_pool.stop(timeout=timeout)
        self.metrics_pool.stop(timeout=timeout)  # 新增

    def get_metrics(self) -> Dict[str, Any]:
        """获取性能指标"""
        return {
            # ... 现有指标 ...
            "metrics": {
                "queue_size": self.metrics_queue.qsize(),
                "submitted": self.metrics.metrics_submitted,
                "queue_full": self.metrics.metrics_queue_full,
                "errors": self.metrics.metrics_errors,
            }
        }
```

**关键点**：
- 创建专用队列（独立容量）
- 初始化策略和Worker池
- 提供公开API方法
- 在`start()`/`stop()`中管理生命周期
- 更新指标统计

---

### 步骤5: 更新配置

**文件**: `app/services/persistence/config.py`

```python
@dataclass
class PersistenceConfig:
    """持久化配置"""

    # 存储目录
    storage_base_dir: Path

    # HLS配置
    hls_workers: int = 2
    raw_fps: float = 30.0
    processed_fps: float = 20.0

    # 告警配置
    alarm_workers: int = 1
    alarm_batch_interval: int = 30
    alarm_cooldown_seconds: int = 60

    # 新增：指标持久化配置
    metrics_workers: int = 2
    metrics_db_url: str = "postgresql://localhost:5432/metrics"
    metrics_batch_size: int = 100

    @classmethod
    def from_settings(cls) -> "PersistenceConfig":
        """从settings加载配置"""
        return cls(
            storage_base_dir=Path("./database"),
            hls_workers=int(os.getenv("CLEANSIGHT_PERSISTENCE__HLS_WORKERS", "2")),
            # ... 现有配置 ...

            # 新增配置项
            metrics_workers=int(os.getenv("CLEANSIGHT_PERSISTENCE__METRICS_WORKERS", "2")),
            metrics_db_url=os.getenv("CLEANSIGHT_PERSISTENCE__METRICS_DB_URL", "postgresql://localhost:5432/metrics"),
            metrics_batch_size=int(os.getenv("CLEANSIGHT_PERSISTENCE__METRICS_BATCH_SIZE", "100")),
        )
```

**关键点**：
- 使用环境变量配置
- 提供合理的默认值
- 保持命名规范一致

---

### 步骤6: 更新模块导出

**文件**: `app/services/persistence/__init__.py`

```python
"""持久化模块"""

from app.services.persistence.manager import PersistenceManager
from app.services.persistence.config import PersistenceConfig
from app.services.persistence.models import (
    HLSPersistenceTask,
    AlarmPersistenceTask,
    MetricsPersistenceTask,  # 新增
    PersistenceMetrics
)

__all__ = [
    "PersistenceManager",
    "PersistenceConfig",
    "HLSPersistenceTask",
    "AlarmPersistenceTask",
    "MetricsPersistenceTask",  # 新增
    "PersistenceMetrics",
]
```

---

## 完整示例：添加指标持久化

### 使用新策略

```python
# 在InferenceManager中使用
class InferenceManager:
    def __init__(self):
        # 初始化persistence_manager（已有）
        persist_config = PersistenceConfig.from_settings()
        self.persistence_manager = PersistenceManager(config=persist_config)

    def _collect_and_persist_metrics(self, client_id: str, task_id: int):
        """收集并持久化性能指标"""
        metrics = [
            {'name': 'fps', 'value': self._calculate_fps()},
            {'name': 'latency_ms', 'value': self._calculate_latency()},
            {'name': 'queue_depth', 'value': self._get_queue_depth()},
        ]

        # 提交到persistence模块
        self.persistence_manager.persist_metrics(
            client_id=client_id,
            task_id=task_id,
            metrics=metrics
        )
```

### 环境变量配置

```bash
# .env文件
CLEANSIGHT_PERSISTENCE__METRICS_WORKERS=2
CLEANSIGHT_PERSISTENCE__METRICS_DB_URL=postgresql://localhost:5432/metrics
CLEANSIGHT_PERSISTENCE__METRICS_BATCH_SIZE=100
```

---

## 最佳实践

### 1. 日志规范

```python
# ✅ 推荐：使用logging
logger = logging.getLogger(__name__)
logger.info("指标已持久化: client=%s, count=%d", client_id, len(metrics))
logger.error("持久化失败: %s", e, exc_info=True)

# ❌ 避免：使用print
print(f"[MetricsStrategy] 指标已持久化")
```

### 2. 异常处理

```python
# ✅ 推荐：捕获具体异常
try:
    self.db_client.write(data)
except ConnectionError as e:
    logger.error("数据库连接失败: %s", e)
    return False
except Exception as e:
    logger.error("未知错误: %s", e, exc_info=True)
    return False

# ❌ 避免：吞掉异常
try:
    self.db_client.write(data)
except:
    pass  # 静默失败，难以排查
```

### 3. 队列容量设计

```python
# 根据预期负载选择容量
self.metrics_queue = queue.Queue(
    maxsize=512  # 高频指标：512
)

self.hls_queue = queue.Queue(
    maxsize=256  # 中频HLS：256
)

self.alarm_queue = queue.Queue(
    maxsize=128  # 低频告警：128
)
```

### 4. Worker数量配置

| 任务类型 | Worker数量 | 理由 |
|---------|-----------|------|
| CPU密集型（编码） | 2-4 | 匹配CPU核心数 |
| IO密集型（HTTP/DB） | 1-2 | 避免过度并发 |
| 混合型 | 2 | 平衡CPU和IO |

### 5. 超时设计

```python
# Worker从队列取任务
task = self.input_queue.get(timeout=0.5)  # 0.5秒超时

# Worker池等待停止
thread.join(timeout=10.0)  # 10秒超时

# 数据库操作
response = self.client.write(data, timeout=5.0)  # 5秒超时
```

### 6. 优雅停止

```python
class MetricsWorker:
    def run(self):
        while not self.stop_event.is_set():  # 检查停止信号
            try:
                task = self.queue.get(timeout=0.5)
                self.process(task)
            except Empty:
                continue  # 超时后继续检查stop_event
```

---

## 测试指南

### 单元测试

```python
# tests/services/persistence/test_metrics_strategy.py

import pytest
from app.services.persistence.strategies.metrics_strategy import MetricsPersistenceStrategy

class TestMetricsStrategy:
    def test_persist_metrics_success(self):
        """测试成功持久化"""
        strategy = MetricsPersistenceStrategy(
            db_connection_string="mock://localhost",
            batch_size=10
        )

        metrics = [
            {'name': 'fps', 'value': 30.5},
            {'name': 'latency', 'value': 15.2}
        ]

        result = strategy.persist_metrics(
            client_id="test_client",
            task_id=123,
            metrics=metrics
        )

        assert result is True

    def test_persist_empty_metrics(self):
        """测试空指标列表"""
        strategy = MetricsPersistenceStrategy(
            db_connection_string="mock://localhost"
        )

        result = strategy.persist_metrics(
            client_id="test_client",
            task_id=123,
            metrics=[]
        )

        assert result is False

    def test_convert_to_points(self):
        """测试数据格式转换"""
        strategy = MetricsPersistenceStrategy(
            db_connection_string="mock://localhost"
        )

        metrics = [{'name': 'fps', 'value': 30.5, 'timestamp': 1234567890}]
        points = strategy._convert_to_points("client_1", 123, metrics)

        assert len(points) == 1
        assert points[0]['tags']['client_id'] == "client_1"
        assert points[0]['fields']['fps'] == 30.5
```

### 集成测试

```python
# tests/services/persistence/test_metrics_integration.py

import queue
import time
from app.services.persistence.manager import PersistenceManager
from app.services.persistence.config import PersistenceConfig

class TestMetricsIntegration:
    def test_end_to_end_persistence(self):
        """测试端到端持久化流程"""
        # 1. 初始化
        config = PersistenceConfig(
            storage_base_dir=Path("./test_data"),
            metrics_workers=1
        )
        manager = PersistenceManager(config)
        manager.start()

        try:
            # 2. 提交任务
            success = manager.persist_metrics(
                client_id="test_client",
                task_id=123,
                metrics=[{'name': 'fps', 'value': 30.5}]
            )
            assert success is True

            # 3. 等待处理
            time.sleep(2)

            # 4. 验证队列已清空
            assert manager.metrics_queue.qsize() == 0

        finally:
            # 5. 清理
            manager.stop()
```

### 压力测试

```python
def test_high_load():
    """测试高负载场景"""
    manager = PersistenceManager(config)
    manager.start()

    # 提交1000个任务
    for i in range(1000):
        manager.persist_metrics(
            client_id=f"client_{i % 10}",
            task_id=i,
            metrics=[{'name': 'fps', 'value': 30.0}]
        )

    # 等待处理完成
    time.sleep(10)

    # 验证没有任务丢失
    metrics = manager.get_metrics()
    assert metrics['metrics']['queue_full'] == 0
```

---

## 故障排查

### 常见问题

#### 1. 队列满导致任务丢失

**症状**：
```
logger.warning("指标队列已满，丢弃任务: %s", client_id)
```

**解决方案**：
```python
# 增加队列容量
self.metrics_queue = queue.Queue(maxsize=1024)  # 从512增加到1024

# 或增加Worker数量
self.metrics_pool = MetricsWorkerPool(
    input_queue=self.metrics_queue,
    strategy=metrics_strategy,
    num_workers=4  # 从2增加到4
)
```

#### 2. Worker线程卡死

**症状**：
- 队列积压不断增长
- Worker日志停止输出

**排查**：
```python
# 添加超时机制
try:
    task = self.input_queue.get(timeout=0.5)  # 确保有超时
except Empty:
    continue  # 定期检查stop_event

# 检查策略方法是否阻塞
def persist_metrics(self, ...):
    try:
        self.db_client.write(data, timeout=5.0)  # 添加超时
    except TimeoutError:
        logger.error("数据库写入超时")
        return False
```

#### 3. 内存泄漏

**症状**：
- 内存占用持续增长
- 进程被OOM Killer杀死

**排查**：
```python
# 检查队列是否无限增长
logger.info("队列大小: %d", self.metrics_queue.qsize())

# 限制任务对象大小
@dataclass
class MetricsPersistenceTask:
    metrics: List[Dict[str, Any]]

    def __post_init__(self):
        # 限制单次任务的数据量
        if len(self.metrics) > 1000:
            raise ValueError("单次任务指标数不能超过1000")
```

#### 4. 数据库连接池耗尽

**症状**：
```
ConnectionError: Too many connections
```

**解决方案**：
```python
class MetricsPersistenceStrategy:
    def __init__(self, db_connection_string: str):
        # 使用连接池
        self.db_pool = create_connection_pool(
            db_connection_string,
            max_connections=10  # 限制连接数
        )

    def persist_metrics(self, ...):
        # 使用上下文管理器自动归还连接
        with self.db_pool.get_connection() as conn:
            conn.write(data)
```

### 调试技巧

#### 1. 启用详细日志

```python
# 临时调整日志级别
import logging
logging.getLogger('app.services.persistence').setLevel(logging.DEBUG)
```

#### 2. 监控队列状态

```python
# 添加监控线程
def _monitor_loop(self):
    while not self._stop_event.is_set():
        logger.info("队列状态: hls=%d, alarm=%d, metrics=%d",
                   self.hls_queue.qsize(),
                   self.alarm_queue.qsize(),
                   self.metrics_queue.qsize())
        time.sleep(10)
```

#### 3. 使用性能分析工具

```bash
# 使用py-spy分析性能瓶颈
pip install py-spy
py-spy top --pid <PID>

# 或使用cProfile
python -m cProfile -o output.prof main.py
```

---

## 参考资料

### 相关文档
- [模块架构设计](./PERSISTENCE_ARCHITECTURE.md)
- [API文档](./PERSISTENCE_API.md)
- [性能调优指南](./PERSISTENCE_TUNING.md)

### 代码示例
- [HLS策略实现](../app/services/persistence/strategies/hls_strategy.py)
- [告警策略实现](../app/services/persistence/strategies/alarm_strategy.py)
- [Manager实现](../app/services/persistence/manager.py)

### 设计模式参考
- [策略模式](https://refactoring.guru/design-patterns/strategy)
- [Worker池模式](https://en.wikipedia.org/wiki/Thread_pool)
- [生产者-消费者模式](https://en.wikipedia.org/wiki/Producer%E2%80%93consumer_problem)

---

## 更新日志

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-01-26 | 1.0.0 | 初始版本 |

---

## 贡献指南

如果你发现文档有误或需要补充，请：
1. 创建Issue描述问题
2. 提交PR修改文档
3. 更新"更新日志"部分

---

**最后更新**: 2026-01-26
**维护者**: CleanSight Backend Team
