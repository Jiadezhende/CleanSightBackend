# 任务状态 WebSocket 接口文档

## 接口地址
```
ws://localhost:8000/task/status/{client_id}
```

## 返回数据格式

### 有活跃任务时
```json
{
  "task_id": 0,
  "status": {
    "code": "running",
    "text": "运行中",
    "message": "清洗任务正在执行",
    "severity": "success"
  },
  "cleaning_step": {
    "code": "3",
    "name": "主清洗"
  },
  "detection": {
    "bending": true,
    "bubble_detected": false,
    "fully_submerged": true
  },
  "messages": [
    "需要弯曲软管"
  ],
  "updated_at": "2025-12-03T10:30:00"
}
```

### 无活跃任务时
```json
{
  "task_id": null,
  "status": {
    "code": "idle",
    "text": "空闲",
    "message": "当前无活跃任务",
    "severity": "info"
  },
  "cleaning_step": null,
  "detection": null,
  "messages": [
    "等待任务启动"
  ],
  "updated_at": null
}
```

## 字段说明

### status 对象
- `code`: 状态码 (idle, running, paused, completed, error, terminated)
- `text`: 状态中文描述
- `message`: 状态消息（前端可直接显示）
- `severity`: 严重程度 (info, warning, error, success) - 可用于前端样式

### cleaning_step 对象
- `code`: 步骤编号 (0-7)
- `name`: 步骤名称
  - 0: 准备阶段
  - 1: 预冲洗
  - 2: 酶洗
  - 3: 主清洗
  - 4: 漂洗
  - 5: 终末漂洗
  - 6: 干燥
  - 7: 完成

### detection 对象
- `bending`: 是否需要弯折软管
- `bubble_detected`: 是否检测到气泡（漏气）
- `fully_submerged`: 是否完全浸没

### messages 数组
前端可直接显示的消息列表，包含：
- 设备运行正常
- 检测到气泡，可能存在漏气！请检查管路密封性
- 需要弯曲软管
- 内镜未完全浸没，请调整位置