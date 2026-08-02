# `/health` — 健康与监控

一组只读 GET 端点，前端用来观察后端全局健康状态：当前有多少客户端在跑、有没有流在重连、监控循环累计干了多少活。数据全部来自内存中的 `GlobalHealthMonitor` 单例（`app/services/health_monitor/monitor.py`），不查 DB、不落盘。

三个端点均无鉴权、正常返回 **200**，且**永不抛异常**：判断可用性只看 `status` 字段（`running` / `not_initialized`），不要靠 HTTP 状态码。通用约定见 [README](README.md)。

> **健康监控未就绪时**（进程刚起、lifespan 尚未构造监控单例，或异常未初始化），三个端点**统一**返回：
> ```jsonc
> { "status": "not_initialized", "message": "Health monitor not initialized" }
> ```
> 此时不含任何 `clients` / `config` / 统计字段。前端必须先判 `status === "running"` 再读后续字段，否则会读到 `undefined`。

---

## GET /health/status

系统整体状态快照，前端（如 `/admin` 概览页）用它一次性拿到「客户端分类计数 + 监控累计统计」。

- `clients` 分类计数是**缓存快照**：每轮监控检查（`check_interval`，默认 1 秒）刷新一次，不是调用时实时计算。
- `monitor_stats` 是**自启动以来的累计计数**（持续增长），外加一份重连中的实时列表。

**请求参数**：无。

### 响应 `200`（就绪时）

```jsonc
{
  "status": "running",
  "clients": {
    "total_clients": 2,      // 有队列（ClientQueues）的客户端总数
    "active_streams": 2,     // 有解码器且不在重连中的客户端数
    "reconnecting": 0,       // 正在重连中的客户端数
    "orphan_streams": 0,     // 有队列但无解码器、且不在重连中的客户端数
    "orphan_decoders": 0     // 有解码器但无队列、且不在重连中的解码器数
  },
  "monitor_stats": {
    "checks": 100,               // 监控循环累计执行次数
    "disconnects": 3,            // 累计检测到断线、进入重连模式的次数（含首启失败）
    "cleanups": 1,               // 累计执行完整清理的次数
    "reconnects": 2,             // 累计发起 respawn 的次数
    "reconnect_successes": 2,    // 累计重连成功的次数
    "orphans_detected": 0,       // 累计检测到孤儿（孤儿流 + 孤儿解码器）的次数
    "reconnecting_clients": []   // 当前重连中的客户端 task_id 列表（实时快照）
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `running`；未就绪时为 `not_initialized`（见顶部说明） |
| `clients.total_clients` | int | 有队列的客户端总数。**注意字段名是 `total_clients` 不是 `total`**（缓存快照，每轮检查刷新） |
| `clients.active_streams` | int | 有解码器且不在重连中的客户端数 |
| `clients.reconnecting` | int | 正在重连中的客户端数 |
| `clients.orphan_streams` | int | 有队列但无解码器、且不在重连中 |
| `clients.orphan_decoders` | int | 有解码器但无队列、且不在重连中 |
| `monitor_stats.checks` | int | 监控循环累计执行次数（**累计**，纯次数无单位） |
| `monitor_stats.disconnects` | int | 累计检测到断线、进入重连模式的次数，**含首启失败**（**累计**，纯次数）。**字段名是 `disconnects`，不是旧版的 `suspects`** |
| `monitor_stats.cleanups` | int | 累计完整清理次数（**累计**，纯次数） |
| `monitor_stats.reconnects` | int | 累计发起 respawn 的次数（**累计**，纯次数） |
| `monitor_stats.reconnect_successes` | int | 累计重连成功次数（**累计**，纯次数） |
| `monitor_stats.orphans_detected` | int | 累计孤儿检测次数（**累计**，纯次数） |
| `monitor_stats.reconnecting_clients` | int[] | 当前重连中的客户端 `task_id` 列表（**实时快照**，可为空数组） |

> **`disconnects` / `reconnects` / `reconnect_successes` 是一组重连计数，读法：**检测到断线 `disconnects` 次 → 发起 respawn `reconnects` 次 → 恢复 `reconnect_successes` 次。断线判据是 decoder 子进程死活（非帧陈旧）。

**分类互斥关系**：`total_clients = active_streams + reconnecting + orphan_streams`（`orphan_decoders` 统计的是「有解码器但无队列」，不在这条等式里，因为它不属于「有队列的客户端」）。

> **不包含 `queues` 字段。** `/health/status` 的响应**只有** `clients` 和 `monitor_stats` 两块（见 `monitor.py` `get_system_status()`），没有各客户端逐条队列深度（`raw_queue_size` / `ready_queue_size` 等）。要逐客户端队列详情走别的途径（`/admin` 相关端点或 InferenceManager 统计），别指望这里有。
>
> `monitor_stats` 里**没有** `reconnecting_count`（那是 `/monitor/stats` 独有的），这里只给 `reconnecting_clients` 列表——数量自己取 `.length`。

### 错误

无。永不抛异常；不可用时返回 200 + `{"status":"not_initialized"}`（见顶部）。

### 前端坑点

- 判可用性只看 `status`，不看 HTTP code（永远 200）。
- `clients` 是缓存快照（每 `check_interval` 刷新，默认 1 秒），不是实时；`reconnecting_clients` 才是实时列表。
- 字段名与直觉/旧版不符：客户端总数是 `total_clients`（不是 `total`）；进入重连累计是 `disconnects`（**旧版叫 `suspects`，已改名**，别再按老字段解析）。
- 参考实现：`/admin` 概览页消费逻辑见 [app/static/admin/index.html](../../app/static/admin/index.html)（`fetchHealth()`）。

---

## GET /health/monitor/stats

只要监控累计计数 + 重连实时快照，不要 `clients` 分类的场景用这个。它比 `/health/status` 多返回一个 `reconnecting_count`（重连中客户端数量），且把各计数**平铺在顶层**（不套 `monitor_stats`）。

**请求参数**：无。

### 响应 `200`（就绪时）

```jsonc
{
  "status": "running",
  "checks": 100,               // 监控循环累计执行次数
  "disconnects": 3,            // 累计检测到断线、进入重连模式的次数（含首启失败）
  "cleanups": 1,               // 累计完整清理次数
  "reconnects": 2,             // 累计发起 respawn 的次数
  "reconnect_successes": 2,    // 累计重连成功次数
  "orphans_detected": 0,       // 累计孤儿检测次数
  "reconnecting_count": 0,     // 当前重连中的客户端数量（实时快照）
  "reconnecting_clients": []   // 当前重连中的客户端 task_id 列表（实时快照）
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `running`；未就绪时 `not_initialized` |
| `checks` | int | 监控循环累计执行次数（**累计**，纯次数） |
| `disconnects` | int | 累计检测到断线、进入重连模式的次数，**含首启失败**（**累计**，纯次数）。旧版名为 `suspects`，已改名 |
| `cleanups` | int | 累计完整清理次数（**累计**，纯次数） |
| `reconnects` | int | 累计发起 respawn 的次数（**累计**，纯次数） |
| `reconnect_successes` | int | 累计重连成功次数（**累计**，纯次数） |
| `orphans_detected` | int | 累计孤儿检测次数（**累计**，纯次数） |
| `reconnecting_count` | int | 当前重连中的客户端数量（**实时快照**，纯次数） |
| `reconnecting_clients` | int[] | 当前重连中的客户端 `task_id` 列表（**实时快照**，可为空数组） |

> 前 6 项累计持续增长；后 2 项是实时快照，反映当下状态。`reconnecting_count === reconnecting_clients.length`，恒等。

### 错误

无。永不抛异常；不可用时 200 + `{"status":"not_initialized"}`。

### 前端坑点

- 计数平铺在**顶层**，不套 `monitor_stats`——与 `/health/status` 里同名字段的嵌套层级不同，别复制粘贴解析逻辑。
- 相比 `/health/status`，这里多一个 `reconnecting_count`（数量），少一整块 `clients` 分类。要客户端分类计数请改用 `/health/status`。
- 这些计数无单位（纯次数），不是时间。
- 断线累计字段名是 `disconnects`（**旧版 `suspects` 已废弃**）。

---

## GET /health/monitor/config

读取监控循环的运行参数（各时间阈值），前端一般用于运维/调试面板展示，不影响业务逻辑。配置在进程启动时从 `config/health_monitor_config.yaml` 一次性加载，运行期不变。

**请求参数**：无。

### 响应 `200`（就绪时）

```jsonc
{
  "status": "running",
  "config": {
    "check_interval": 1.0,        // 监控循环间隔（秒）
    "heartbeat_timeout": 5.0,     // 重连成功的新帧新鲜度阈值（秒）
    "reconnect_interval": 5.0,    // 两次 respawn 之间的间隔（秒）
    "cleanup_timeout": 20.0,      // 无帧多久放弃重连并清理（秒）
    "orphan_timeout": 30.0        // 孤儿流空闲超时（秒），超时即清理
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `running`；未就绪时 `not_initialized` |
| `config.check_interval` | float | 监控循环间隔，**秒**（默认 1.0） |
| `config.heartbeat_timeout` | float | 重连成功的新帧新鲜度阈值，**秒**（默认 5.0）；重连后新帧需比此更新才判定恢复。**注意：断流判定已不看它**——断流现以 decoder 子进程死活为准，不再靠帧陈旧超时 |
| `config.reconnect_interval` | float | 两次 respawn 尝试之间的节流间隔，**秒**（默认 5.0） |
| `config.cleanup_timeout` | float | 无帧多久放弃重连并执行完整清理，**秒**（默认 20.0）。**这是一等配置项，可在 YAML 直接配**（`monitor.cleanup_timeout`） |
| `config.orphan_timeout` | float | 孤儿流空闲超时，**秒**（默认 30.0） |

> **响应只有一个 `config` 块，没有 `derived` 块。** 旧版曾有 `derived` 子块（`suspect_timeout` / `cleanup_timeout` 派生量），现已删除：`cleanup_timeout` 提为一等配置项后，派生式退化成 config 的恒等副本，回显纯属冗余（见 `monitor.py` `get_monitor_config()` 注释）。
>
> **不再有 `max_reconnect_attempts`。** 重连已不数次数、无尝试上限，仅由 `cleanup_timeout`（纯时间）收口；该字段在 config 回显里也已删除。
>
> 注意：`config` 里**不含** `task_max_duration`（任务最大运行时长，配置层存在但此端点不暴露）；也不含 lifespan 日志里出现的其它内部量。以本表为准。

### 错误

无。永不抛异常；不可用时 200 + `{"status":"not_initialized"}`。

### 前端坑点

- 所有时间字段单位统一为**秒**（float），与告警/traceback 的毫秒、段文件的微秒不同，别混用。
- `config` 是启动时加载的静态快照，运行期不会变，可放心缓存。
- **旧版的 `derived` 块、`max_reconnect_attempts` 字段均已移除，别再解析。** `cleanup_timeout` 现在直接在 `config` 块里、是可配的一等项（默认 20.0s），不再是派生量。

---

## 附：静默失败与易误判情况

以下情况后端**不报错**、返回 200，排查时容易误判为 bug：

| 现象 | 后端实际状态 |
|------|------------|
| 拿到 200 但只有 `{"status":"not_initialized","message":...}` | 健康监控单例尚未就绪（进程刚起 / lifespan 未构造 / 异常未初始化）。**不是** 404，先判 `status` 再读字段 |
| `/health/status` 里找不到 `queues` 字段 | 本端点从不返回逐客户端队列深度；该字段不存在，非丢失 |
| `monitor_stats` / stats 里找不到 `suspects` | 已改名为 `disconnects`；按旧字段解析会拿到 `undefined`，非埋点失效 |
| `/monitor/config` 里找不到 `derived` 块或 `max_reconnect_attempts` | 均已删除；`cleanup_timeout` 现为 `config` 块内一等项，非丢失 |
| `clients` 计数与刚发生的操作对不上（有延迟） | `clients` 是每 `check_interval`（默认 1 秒）刷新的缓存快照，非实时；`reconnecting_clients` 才实时 |
| `monitor_stats` 里没有 `reconnecting_count` | 该字段是 `/monitor/stats` 独有；`/health/status` 只给 `reconnecting_clients` 列表，数量取 `.length` |
| 各累计计数长期为 0 | 系统一直健康、无断流/无孤儿，监控没触发过任何动作（只有 `checks` 会随循环增长）——正常，非埋点失效 |

参考实现：`/admin` 概览页消费 `/health/status` 的逻辑见 [app/static/admin/index.html](../../app/static/admin/index.html)（`fetchHealth()`）。
