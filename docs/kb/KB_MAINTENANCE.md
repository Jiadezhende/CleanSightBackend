> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 知识库维护规范

## 可信来源顺序

1. 当前代码：`app/`、`mediamtx_gateway/`、`scripts/`。
2. 当前配置：`config/*.yaml`、`.env.example`、`mediamtx/mediamtx.yml`、启动脚本。
3. 当前测试：`tests/`、`integration_tests/`。
4. 旧 `docs/`：只能作为待核验线索，必须在代码或测试中找到依据后才能写入本知识库。

## 文件规范

- 所有文件必须平铺在 `docs/kb/` 下。
- 所有文件顶部必须包含统一三行元信息：更新时间、依据来源、可信级别。
- `INDEX.md` 是唯一入口；新增文件后必须更新 `INDEX.md`。
- 业务结论必须配代码来源路径，不能只写口头理解。
- 对尚未由代码验证的推测，用“待核验”标注，不写成事实。

## 更新时间规则

- 修改任意知识库文件时，更新该文件顶部的 `更新时间`。
- 批量更新时，所有被修改文件使用同一天日期。
- 首版统一使用 `2026-05-24`。

## 内容粒度

- 优先写稳定知识：职责边界、数据流、状态归属、关键约束。
- 避免复制大段代码或旧文档。
- API、配置、检测阈值等易变信息要标注来源文件。
- 与业务强相关的检测标准应同时写明“当前代码实现”和“可调整位置”。

## 静态检查建议

```bash
find docs/kb -maxdepth 1 -name '*.md' -print | sort
# 列出每个文件的更新时间头，人工核对是否为最近批次日期
grep -h '^> 更新时间：' docs/kb/*.md | sort | uniq -c
```

## 主要代码来源

- 应用入口：`app/main.py`
- 统一 API：`app/routers/api.py`
- 运行编排：`app/services/run_control.py`
- 共享契约：`app/domain/`
- 推理服务：`app/services/inference/`
- 流服务：`app/services/stream/`
- 客户端状态：`app/services/client/`
- 持久化：`app/services/persistence/`
- 健康监控：`app/services/health_monitor/`
- 追溯与媒体：`app/routers/traceback.py`、`app/routers/media.py`、`app/services/traceback/`
- Lab：`app/routers/lab.py`、`app/services/lab/`
- Gateway：`app/utils/gateway.py`、`mediamtx_gateway/`

