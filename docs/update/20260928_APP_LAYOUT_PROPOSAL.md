# app 目录结构提案：每层自带通用能力，storage 只出 query / insert，DB 独立成层

> **变更状态**：提案（2026-09-28）——只定目录落点，逐项调整后再分批落地
> **知识库**：无需沉淀（提案本身；结论随各批记录沉淀）

## 概述

给 `app/` 定目标目录结构：通用能力按使用范围就近落到所在层的 `utils/`，顶层 `app/utils/` 取消——
`gateway` 提到 `app/` 根，`exceptions` 并入契约包，契约包 `domain/` 改名 `types/`；
DB 从根目录移到独立的 `db/`；router 里散落的数据读取与业务流程按归属回收到 storage / db / services。

## 目录结构

图例：`←` 从哪里迁来；`→` 迁到哪里去；`[待定]` 尚未决定。

```text
app/
  main.py                  组装入口：lifespan 嵌套顺序、中间件、路由注册、异常 → HTTP 映射
  settings.py              配置
  gateway.py               ASGI 网关中间件 + IP 白名单 / 限流 ← app/utils/gateway.py（mediamtx_gateway 进程共用）
  static/                  前端静态资产（不变）

  types/                   跨层共用的契约 ← app/domain/（改名）
    frame.py
    detection.py
    temporal.py
    alarm.py
    run.py
    exceptions.py            AppError 体系 ← app/utils/exceptions.py
    render.py              → services/inference/online/（只有 inference.online 用）

  routers/                 HTTP 层：参数 / DTO / 状态码 / token / URI，组合下层能力
    utils/                   本层通用
      runs.py                  ← routers/_runs.py，并入 media._resolve_run、admin._no_run
      media_token.py           ← services/traceback/media_token.py（只有 routers/{media,traceback} 用）
    api.py
    ai.py
    task.py
    traceback.py
    media.py
    lab.py                   送标流程 → services/lab/
    admin.py
    health.py
    algorithm.py

  daemons/                 按时钟自驱的后台任务：不属于任何 run、没有调用方向它下发工作；可依赖 services
                           routers 只许读它的状态（运行与否 / 统计 / 配置），不许下发命令
    health_monitor/          ← services/health_monitor/（依赖 5 个服务单例 + run_controller，无服务反向依赖）
                               manager.py → worker.py；GlobalHealthMonitor → HealthMonitorWorker
    cleanup/                 ← services/persistence/workers/cleanup_worker.py（盘上产物 TTL 清理，只依赖 storage）
                               StorageCleanupWorker → CleanupWorker，单例 cleanup_worker

  services/                由 run 或请求驱动：有活体（线程 / 进程 / 单例 / lifespan）或有外部副作用的逻辑
    utils/                   本层通用
      task_queue.py            ← app/utils/task_queue.py
      worker_guard.py          ← app/utils/worker_guard.py
      pressure.py              ← app/utils/pressure.py
      metrics.py               ← app/utils/metrics.py
      vod_playlist.py          （不变）
      media_timeline.py        拆分：媒体轴（段落点 / 总长 / 墙钟↔媒体换算）→ storage/hls/
                               断流判定（GAP_THRESHOLD_MS 阈值）留在本层
    run_control/             跨服务编排 ← services/run_control.py
                               RunController → RunControlService，单例移入 instance.py
    client/                  manager.py → service.py；ClientManager → ClientService，client_manager → client_service
                               单例移入 instance.py；__init__ 不再导出单例
    stream/                  manager.py → service.py
                             app/utils/decorators.py 删除（log_call 只有 stream 用，且身份提取从未生效）
    alarm/                   ← services/persistence/（改名：只做告警上报）
                               manager.py → service.py；PersistenceManager → AlarmService
                               strategies/alarm_strategy.py → reporter.py
                               workers/alarm_worker.py → alarm_worker.py
                             app/utils/executor.py 删除，告警重试就地写在 alarm_worker
    recording/               _sweeper.py → sweep_worker.py
    inference/
      online/                manager.py → service.py；InferenceManager → InferenceService
        render.py              ← app/domain/render.py
        visualization/         pool.py + worker.py → visualization_worker.py
    lab/                     + 送标流程 ← routers/lab.py（校验 → 剪片 → 上传 → 清理）
                             config.py → runtime_config.py（页面可改的运行时状态，不是配置读取）
                             __init__ 清掉重新导出
    algorithm/
    traceback/               删除：media_token.py → routers/utils/
    temp/colorstrip/         → ref/colorstrip/（仓库根，实验脚本 + 样例图，不属于产品包）

  storage/                 盘上产物；碰盘的函数只有 query_* / insert_*
    utils/                   本层通用
      fs.py                    ← storage/_fs.py
      root.py                  ← storage/_root.py
    runs.py
    tasks.py
    hls/                     + 媒体轴查询 ← services/utils/media_timeline.py（不含断流阈值）
                             + 段时间跨度 / 有无 raw 段等查询 ← routers/{task,traceback,lab}.py
    inference/               + 有无离线结果查询 ← routers/lab.py

  db/                      平台 DB，只读；函数只有 query_*
    database.py              ← app/database.py
    tasks.py                 DBTask 表映射 + clean_task 查询 ← app/models.py、routers/{api,task,lab}.py
    alarms.py                DBAlarm 表映射 + clean_alarm 查询 ← app/models.py、routers/traceback.py
                             （app/models.py 拆分后删除）
```

通用能力的落点按使用范围分三档：

```text
只有一个包用          → 留在那个包里
同一层 ≥2 个包用      → <层>/utils/
跨层                  → 契约与异常进 types/，其余放 app/ 根
```

## services 包骨架

一个角色一个名字，子包同骨架：

```text
services/<svc>/
  __init__.py        纯 docstring；有活体时加 lifespan()。零重新导出，调用方一律走深路径
  service.py         入口：活体类 <Svc>Service；无活体的包放模块函数，文件名不变
  instance.py        单例 <svc>_service 唯一定义处；无活体则没有此文件
  config.py          只读：启动时从 config/ 读本服务的 yaml，产出配置 dataclass
  types.py           本包私有数据形状
  <what>_worker.py   线程 / 进程体：文件名以 _worker 结尾，放包根，不建 workers/ 子包
  <noun>.py          无状态能力模块，名词命名（clip_builder、label_studio_client…）
  impl/              按配置经 importlib 加载的可插拔实现
  cli.py             python -m 入口，单向出口：包内任何模块不许 import 它
  <sub>/             子包，同骨架
```

- 不用下划线前缀：包的公开面由角色文件决定。
- 不建单文件子包：只有一个文件就摊平到包根。

## daemons 包骨架

与 services 骨架相同，只换入口文件的角色名：

```text
daemons/<name>/
  __init__.py        纯 docstring + lifespan()
  worker.py          入口：后台线程类 <Name>Worker
  instance.py        单例 <name>_worker 唯一定义处
  config.py / types.py   同 services
```
