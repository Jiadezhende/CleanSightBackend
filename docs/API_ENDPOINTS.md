# API 端点参考（补充）

本文档补充列出了一些已实现但尚未在 docs 中总结的接口，方便快速查看。

## 获取任务告警记录

- URL: `GET /task/{task_id}/alarms`
- 描述: 查询本地数据库 `alarm_record` 表中为指定 `task_id` 保存的所有告警记录，按 `created_at` 降序返回。用于任务回溯与告警审计。
- 路径参数:
  - `task_id` (int): 任务 ID
- 成功响应示例:

```json
{
  "task_id": 1,
  "total": 2,
  "alarms": [
    {
      "id": 123,
      "task_id": 1,
      "step_id": "0",
      "alarm_type": "流程违规",
      "alarm_level": "high",
      "alarm_message": "检测到未按规范操作：操作员未佩戴手套",
      "alarm_time": "2025-12-08T20:30:15",
      "detection_result": {"detected_objects": ["person","glove"], "confidence": 0.95},
      "camera_ip": "192.168.1.64",
      "reader_ip": "172.16.77.221",
      "created_at": "2025-12-08T20:30:20"
    }
  ]
}
```

- cURL 示例:

```bash
curl -X GET "http://localhost:8000/task/1/alarms"
```

- 注意:
  - FastAPI 的自动文档仍然可用: `http://<host>:<port>/docs`。
  - 当前 `alarm_record` 表由运行时逻辑创建并使用 PostgreSQL 语法（`JSONB`, `SERIAL`）。若使用其他数据库，请确保兼容或迁移为 ORM + Alembic 管理。

