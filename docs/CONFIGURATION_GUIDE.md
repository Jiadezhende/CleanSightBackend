# CleanSight Backend 配置指南

## 配置文件分工原则

CleanSight Backend 使用**双层配置架构**，明确分离基础环境配置和推理业务配置：

```
┌─────────────────────────────────────────────────────────────┐
│                    配置层次架构                              │
├─────────────────────────────────────────────────────────────┤
│  .env                    │  app/config/stages_config.yaml   │
│  基础环境配置             │  推理业务配置                     │
│  ─────────────────       │  ─────────────────────────       │
│  • 数据库连接             │  • 模型推理参数                   │
│  • 服务器地址/端口        │  • Stage 定义                    │
│  • DEBUG 模式             │  • 时序分析器配置                 │
│  • 模型文件路径(可选)     │  • 告警触发条件                   │
│                          │  • batch_size / decimation       │
└──────────────────────────┴──────────────────────────────────┘
```

---

## 1️⃣  .env - 基础环境配置

### 用途
配置**部署环境相关的基础参数**，这些参数通常：
- 在不同部署环境（开发/测试/生产）中会改变
- 涉及敏感信息（数据库密码、API密钥）
- 不应该提交到版本控制系统

### 配置内容

#### ✅ 应该在 .env 中配置的

| 配置项 | 说明 | 示例 |
|--------|------|------|
| **数据库配置** | PostgreSQL 连接信息 | `CLEANSIGHT_DB_HOST=localhost` |
| **服务器配置** | FastAPI 服务器地址和端口 | `CLEANSIGHT_SERVER_HOST=0.0.0.0` |
| **调试模式** | 是否启用 DEBUG 日志 | `CLEANSIGHT_DEBUG=false` |
| **模型路径** | 模型文件的磁盘路径（可选） | `CLEANSIGHT_YOLO_MODEL_PATH=/data/models/yolo.pt` |
| **外部 API** | 告警上报 URL 等（可选） | `CLEANSIGHT_ALARM_REPORT_URL=http://...` |

#### ❌ 不应该在 .env 中配置的

- ~~推理阈值~~（conf_threshold, iou_threshold）→ 放到 `stages_config.yaml`
- ~~batch_size~~、~~decimation~~ → 放到 `stages_config.yaml`
- ~~Stage 定义~~、~~模型绑定~~ → 放到 `stages_config.yaml`

### 配置示例

```bash
# .env

# 数据库配置
CLEANSIGHT_DB_HOST=116.204.65.72
CLEANSIGHT_DB_PORT=5432
CLEANSIGHT_DB_NAME=aidkdb
CLEANSIGHT_DB_USER=aidk
CLEANSIGHT_DB_PASSWORD=your_password

# 模型路径（可选，会被 yaml 引用）
CLEANSIGHT_YOLO_MODEL_PATH=/opt/models/yolo-best.pt
CLEANSIGHT_BUBBLE_MODEL_PATH=/opt/models/bubble-best.pt

# 告警 URL（可选）
CLEANSIGHT_ALARM_REPORT_URL=http://116.204.65.72:8881/gdmp/v1/api/nt/alarm_report
```

---

## 2️⃣  stages_config.yaml - 推理业务配置

### 用途
配置**推理流程的详细业务逻辑**，这些参数通常：
- 由算法工程师调整优化
- 在不同场景下需要灵活修改
- 可以安全地提交到版本控制系统
- **修改后无需重启服务**（未来支持热加载）

### 配置内容

#### ✅ 应该在 stages_config.yaml 中配置的

| 配置项 | 说明 | 示例 |
|--------|------|------|
| **Stage 定义** | 定义推理阶段（LEAK, CLEAN 等） | `stages: LEAK:` |
| **模型列表** | 每个 Stage 使用哪些模型 | `models: - name: bubble_detection` |
| **推理参数** | 置信度阈值、IoU 阈值等 | `conf_threshold: 0.5` |
| **时序分析器** | 时序逻辑（连续帧、滑动窗口） | `mode: consecutive` |
| **告警触发条件** | 何时触发告警 | `condition: bubble_detected == True` |
| **性能参数** | batch_size、降帧率等 | `batch_size: 4` |

#### ❌ 不应该在 stages_config.yaml 中配置的

- ~~数据库连接信息~~ → 放到 `.env`
- ~~服务器端口~~ → 放到 `.env`
- ~~敏感密码、密钥~~ → 放到 `.env`

### 配置示例

```yaml
# app/config/stages_config.yaml

stages:
  LEAK:
    models:
      - name: bubble_detection
        class: app.services.ai_models.bubble_task.BubbleDetectionTask
        params:
          model_path: ${BUBBLE_MODEL_PATH:./weights/bubble.pt}  # 引用 .env 变量
          conf_threshold: 0.5    # ✅ 推理参数在 yaml 中配置
          iou_threshold: 0.45
          enabled: true

    temporal_analyzer:
      class: app.services.inference.temporal_analyzer.DefaultTemporalAnalyzer
      config:
        bubble:
          mode: consecutive      # ✅ 时序逻辑在 yaml 中配置
          threshold: 3

    alarm_triggers:
      - condition: bubble_detected == True  # ✅ 告警条件在 yaml 中配置
        alarm_type: 流程违规
        alarm_message: 检测到气泡异常

global:
  batch_size: 4              # ✅ 性能参数在 yaml 中配置
  inference_decimation: 2
```

---

## 3️⃣  配置交互：环境变量引用

YAML 配置可以引用 .env 中定义的环境变量：

```yaml
# stages_config.yaml

params:
  model_path: ${BUBBLE_MODEL_PATH:./weights/bubble.pt}
  #           └─────┬──────┘ └──────┬────────┘
  #           引用环境变量      默认值（如果未设置）
```

**工作流程**：
1. 系统读取 `.env` → 加载环境变量到内存
2. 系统读取 `stages_config.yaml` → 遇到 `${VAR}` 时替换为环境变量
3. 如果环境变量未设置 → 使用冒号后的默认值

---

## 4️⃣  修改配置的最佳实践

### 修改推理参数（如阈值、batch_size）

1. 只需编辑 `app/config/stages_config.yaml`
2. 无需修改 `.env`
3. 未来支持热加载（无需重启服务）

```yaml
# 示例：降低气泡检测的置信度阈值
models:
  - name: bubble_detection
    params:
      conf_threshold: 0.3  # 从 0.5 改为 0.3
```

### 修改模型文件路径

**方式 1（推荐）**：只修改 `.env`
```bash
# .env
CLEANSIGHT_BUBBLE_MODEL_PATH=/new/path/bubble-v2.pt
```

**方式 2**：修改 yaml 中的默认值
```yaml
# stages_config.yaml
params:
  model_path: ${BUBBLE_MODEL_PATH:./weights/bubble-v2.pt}
```

### 修改数据库连接

只需编辑 `.env`：
```bash
# .env
CLEANSIGHT_DB_HOST=new-db-host.com
CLEANSIGHT_DB_PASSWORD=new-password
```

---

## 5️⃣  配置验证

启动服务时，系统会打印配置加载日志：

```
[ConfigLoader] 成功加载配置文件: app/config/stages_config.yaml
[InferenceManager] 从配置文件加载 2 个 stage
[ComponentFactory] 成功创建模型: bubble_detection
[ComponentFactory] 成功创建模型: bending_detection
```

如果配置有误，会回退到默认配置：
```
[InferenceManager] 加载配置文件失败: xxx，使用默认配置
```

---

## 6️⃣  总结对比表

| 特性 | .env | stages_config.yaml |
|------|------|-------------------|
| **配置类型** | 环境基础参数 | 推理业务逻辑 |
| **典型内容** | 数据库、服务器、密码 | 模型参数、阈值、Stage定义 |
| **版本控制** | ❌ 不提交（`.gitignore`） | ✅ 提交到 Git |
| **修改频率** | 低（部署时设置一次） | 高（调参优化） |
| **修改角色** | 运维工程师 | 算法工程师 |
| **热加载** | ❌ 需要重启服务 | ✅ 未来支持热加载 |
| **敏感信息** | ✅ 包含（密码、密钥） | ❌ 无敏感信息 |

---

## 7️⃣  常见问题 FAQ

### Q1: 为什么不把所有配置都放在 .env？
**A:** `.env` 文件包含敏感信息，不能提交到 Git。推理参数需要频繁调整和版本管理，应该放在 YAML 中。

### Q2: 为什么模型路径在 .env 中定义？
**A:** 模型文件的磁盘路径在不同环境（开发机、服务器）下不同，属于环境配置。但 YAML 中可以设置默认值作为后备。

### Q3: 如何添加新的 Stage 或模型？
**A:** 只需编辑 `stages_config.yaml`，无需修改代码或 `.env`：

```yaml
stages:
  NEW_STAGE:  # 添加新 Stage
    models:
      - name: new_model
        class: app.services.ai_models.new_task.NewTask
        params:
          model_path: ./weights/new.pt
          conf_threshold: 0.6
```

### Q4: 配置文件损坏了怎么办？
**A:** 系统会自动回退到硬编码的默认配置，保证服务正常运行。

---

## 📚 相关文档

- [配置驱动架构设计](./CONFIG_DRIVEN_ARCHITECTURE.md)
- [快速开始指南](./QUICK_START.md)
- [组件工厂使用](./CONFIG_INTEGRATION_STATUS.md)
