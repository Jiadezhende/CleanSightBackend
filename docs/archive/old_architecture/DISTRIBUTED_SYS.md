# 分布式部署设计方案

在多个节点上部署该后端系统，worker节点负责推理、转发，用heartbeat发送当前管理的clients列表与推理任务数量，便于master节点实现负载均衡。

## 部署架构

采用Master-Worker分布式架构：

- **Master节点**：负责负载均衡、任务分配、监控Worker状态。接收客户端请求，分配最合适的Worker节点IP给客户端。
- **Worker节点**：负责实际的AI推理、视频流处理、WebSocket推送。每个Worker独立管理自己的客户端队列和推理任务。
- **客户端**：摄像头客户端或前端应用，通过Master获取可用Worker IP，然后直接连接Worker进行视频流处理。

### 架构图

```
[客户端] --> [Master节点] --> 分配Worker IP --> [Worker节点]
     |              |                           |
     |              |--- 监控Heartbeat ---|     |
     |              |                           |
     |--- 直接连接 ---|                           |
                    [Worker节点] (推理、推送)
```

## 组件角色

### Master节点

- **职责**：
  - 接收客户端的节点分配请求
  - 维护Worker节点列表和状态
  - 实现负载均衡算法，选择最优Worker
  - 监控Worker健康状态（通过Heartbeat）
  - 处理Worker注册/注销
  - **维护全局客户端-Worker映射**：记录所有客户端连接到哪个Worker，便于中控台查询和管理

- **API接口**：
  - `GET /master/get_worker`: 获取可用Worker IP
  - `POST /master/register_worker`: Worker注册
  - `POST /master/heartbeat`: Worker心跳更新
  - `GET /master/clients`: 获取所有客户端状态（中控台使用）
  - `GET /master/client_worker_map`: 获取客户端-Worker映射KV对

### Worker节点

- **职责**：
  - 执行AI推理任务
  - 处理RTMP/RTSP视频流
  - 管理客户端队列
  - 通过WebSocket推送结果
  - 定期发送Heartbeat到Master
  - **报告客户端状态变化**：客户端连接/断开时通知Master更新全局映射

- **Heartbeat内容**：
  ```json
  {
    "worker_id": "worker_001",
    "ip": "192.168.1.100",
    "port": 8000,
    "clients_count": 5,
    "tasks_count": 12,
    "cpu_usage": 0.75,
    "memory_usage": 0.60,
    "last_heartbeat": 1640995200,
    "clients": [
      {"client_id": "camera_001", "status": "active", "task_id": 123},
      {"client_id": "camera_002", "status": "idle", "task_id": null}
    ]
  }
  ```

## 负载均衡策略

### 算法选择

1. **轮询 (Round Robin)**：简单轮询分配，适合负载均匀的情况
2. **最少连接 (Least Connections)**：选择当前客户端数量最少的Worker
3. **最少任务 (Least Tasks)**：选择当前推理任务数量最少的Worker
4. **加权轮询 (Weighted Round Robin)**：根据Worker性能设置权重

### 推荐策略

采用**最少任务 + CPU/内存权重**的复合策略：

```python
def select_worker(workers):
    # 过滤健康Worker
    healthy_workers = [w for w in workers if w.is_healthy()]
    
    if not healthy_workers:
        return None
    
    # 计算权重得分 (任务数 * 0.5 + CPU使用率 * 0.3 + 内存使用率 * 0.2)
    scored_workers = []
    for w in healthy_workers:
        score = (w.tasks_count * 0.5 + 
                w.cpu_usage * 0.3 + 
                w.memory_usage * 0.2)
        scored_workers.append((w, score))
    
    # 返回得分最低的Worker
    return min(scored_workers, key=lambda x: x[1])[0]
```

## 客户端请求流程

1. **获取Worker IP**：
   ```bash
   curl -X GET "http://master-ip:8000/master/get_worker"
   # 响应: {"worker_ip": "192.168.1.100", "worker_port": 8000}
   ```

2. **连接Worker**：
   - 使用返回的IP直接连接Worker节点
   - 进行RTMP/RTSP流处理、AI推理等操作

3. **错误处理**：
   - 如果Worker不可用，客户端重新请求Master获取新Worker
   - Master检测到Worker故障时，从列表中移除

## 部署步骤

### 1. 环境准备

- 每个节点安装Docker和Docker Compose
- 配置网络，确保Master和Worker间可通信
- 设置防火墙规则

### 2. Master节点部署

```yaml
# docker-compose.master.yml
version: '3.8'
services:
  master:
    build: .
    ports:
      - "8000:8000"
    environment:
      - ROLE=master
      - WORKERS_FILE=/app/workers.json
    volumes:
      - ./workers.json:/app/workers.json
    command: uvicorn app.master:app --host 0.0.0.0 --port 8000
```

### 3. Worker节点部署

```yaml
# docker-compose.worker.yml
version: '3.8'
services:
  worker:
    build: .
    ports:
      - "8000:8000"
    environment:
      - ROLE=worker
      - MASTER_IP=192.168.1.10
      - WORKER_ID=worker_001
    volumes:
      - ./data:/app/data
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 4. 启动顺序

1. 启动Master节点
2. 启动Worker节点（Worker会自动注册到Master）
3. 客户端开始请求

## 监控和故障处理

### 健康检查

- **Heartbeat间隔**：30秒
- **超时时间**：90秒（3次心跳丢失）
- **故障检测**：Master标记Worker为不健康，从分配列表移除

### 故障恢复

- **Worker重启**：自动重新注册到Master
- **Master故障**：使用备用Master或手动切换
- **网络分区**：Worker继续工作，待网络恢复后重新注册

### 日志和监控

- 集中日志收集（ELK Stack）
- 性能指标监控（Prometheus + Grafana）
- 告警系统（当Worker负载过高或故障时）

## 安全考虑

- **认证**：Worker注册时使用API密钥
- **网络安全**：使用HTTPS，内部网络隔离
- **访问控制**：限制Master API访问IP范围
- **数据加密**：敏感配置使用环境变量或密钥管理服务

## 扩展性

- **水平扩展**：动态添加Worker节点
- **垂直扩展**：升级Worker硬件配置
- **异构环境**：支持不同配置的Worker（根据权重调整）
- **多区域部署**：跨数据中心部署，考虑网络延迟

## 中控台设计

### 功能概述

中控台是一个Web界面，用于集中监控和管理分布式系统中的所有客户端和Worker节点：

- **实时状态查看**：显示所有客户端的运行状态、连接的Worker、任务进度
- **全局映射管理**：维护client_id -> worker_id的KV映射，便于快速定位和管理
- **负载均衡监控**：可视化各Worker的负载情况，支持手动调整分配
- **告警管理**：显示系统异常、客户端离线等事件
- **操作控制**：支持远程重启客户端、切换Worker等管理操作

### 数据存储方案

#### 是否需要Redis？

**推荐使用Redis**作为Master节点的数据存储后端，主要原因：

1. **高性能内存存储**：全局client-worker映射需要频繁读写，Redis的内存特性提供微秒级访问速度
2. **发布订阅机制**：支持实时状态更新推送给中控台，无需轮询
3. **持久化支持**：RDB/AOF持久化确保Master重启后数据不丢失
4. **分布式扩展**：未来支持多Master节点时，Redis Cluster提供高可用性
5. **原子操作**：支持事务和Lua脚本，确保映射更新的原子性

#### Redis使用场景

- **全局映射存储**：`client_worker_map`哈希表存储client_id到worker信息的映射
- **实时状态广播**：发布订阅频道推送Worker状态变化到中控台
- **缓存Worker列表**：存储所有注册Worker的详细信息和健康状态
- **分布式锁**：防止多个Master同时分配同一个Worker给客户端

#### 替代方案对比

| 方案 | 优点 | 缺点 | 适用场景 |
|------|------|------|----------|
| **内存字典 + 文件持久化** | 简单，无额外依赖 | 重启丢失实时数据，文件IO性能低 | 小规模单Master部署 |
| **PostgreSQL/MySQL** | ACID事务，复杂查询 | 读写性能不如Redis，连接开销大 | 需要复杂关系查询的场景 |
| **Redis** | 高性能，发布订阅，持久化 | 增加部署复杂度 | **推荐：分布式系统，高并发实时更新** |
| **Etcd/Consul** | 强一致性，服务发现 | 学习成本高，性能略低于Redis | Kubernetes环境，服务网格 |

#### Redis部署建议

```yaml
# docker-compose.redis.yml
version: '3.8'
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
      - ./redis.conf:/etc/redis/redis.conf
    command: redis-server /etc/redis/redis.conf

volumes:
  redis_data:
```

**Redis配置要点**：
- 启用AOF持久化确保数据安全
- 设置适当的内存限制和淘汰策略
- 配置密码认证和网络访问控制
- 启用Redis Cluster以支持高可用

### 全局Client-Worker映射

Master节点维护一个全局的KV映射结构：

```python
# Redis存储示例
import redis

r = redis.Redis(host='localhost', port=6379, db=0)

# 存储映射关系
r.hset('client_worker_map', 'camera_001', json.dumps({
    "worker_id": "worker_001",
    "worker_ip": "192.168.1.100",
    "status": "active",
    "last_seen": 1640995200,
    "task_id": 123
}))

# 获取映射
client_info = json.loads(r.hget('client_worker_map', 'camera_001'))
```

### 映射维护机制

1. **客户端连接**：
   - 客户端请求Master获取Worker IP
   - Master分配Worker后，在Redis中记录映射关系
   - Worker确认客户端连接后，通过Heartbeat更新Redis中的映射状态

2. **状态更新**：
   - Worker在Heartbeat中报告所有连接的客户端列表
   - Master批量更新Redis中的全局映射
   - 通过Redis发布订阅通知中控台状态变化

3. **客户端断开**：
   - Worker检测到客户端断开，通过Heartbeat通知Master
   - Master更新Redis映射状态，标记为offline
   - 发布状态变更事件

4. **Worker故障**：
   - Master检测Worker故障后，将其所有客户端在Redis中标记为需要重新分配
   - 客户端重新请求Master获取新Worker

### 中控台API接口

- `GET /console/dashboard`: 从Redis获取仪表板数据（所有客户端、Worker状态汇总）
- `GET /console/clients`: 获取Redis中所有客户端详细信息
- `GET /console/client/{client_id}`: 获取特定客户端在Redis中的状态和映射信息
- `POST /console/client/{client_id}/reassign`: 手动更新Redis中的客户端分配
- `GET /console/workers`: 获取所有Worker在Redis中的负载情况
- `WebSocket /console/updates`: 订阅Redis发布订阅频道，接收实时状态更新推送