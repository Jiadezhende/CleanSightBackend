> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Testing Map

本文件索引当前测试覆盖面，并给后续修改提供优先补测方向。

## API 与并发

- `tests/test_api_concurrency.py`：并发 start、任务切换、不同 client 不互阻、terminate 锁。
- `tests/test_task_message_api.py`：实时消息接口。
- `tests/test_alarm_increment.py`：告警 gate、seq、自增和任务切换重置。

## 推理与时序

- `tests/test_inference_stage_routing.py`：`current_step` 到 stage 路由。
- `tests/test_temporal_debounce.py`：时序去抖逻辑。
- `tests/test_boundary_layers.py`：边界层行为。
- `tests/test_exception_handling.py`：异常分类和处理。

## 流与重连

- `tests/test_stream_rewrite.py`：RTSP URL 内部端口改写。
- `tests/test_reconnect_on_initial_failure.py`：初始拉流失败后重连。
- `integration_tests/deprecated/*reconnect*`：旧集成测试线索，运行前需确认可用性和环境。

## Gateway

- `tests/test_gateway.py`：ASGI Gateway 行为。
- `tests/test_mediamtx_gateway.py`：MediaMTX Gateway 行为。

## 追溯与媒体

- `tests/test_traceback_router.py`：追溯路由。
- `tests/test_traceback_segment_finder.py`：段定位。
- `tests/test_traceback_media_token.py`：媒体 token。

## Lab

- `tests/test_lab_clip_builder.py`：Lab 裁剪构建。

## 建议补测

- 新增检测任务时，补 Detector/Analyzer 单元测试和 YAML 加载测试。
- 修改 HLS 写入逻辑时，补 playlist EXTINF、在途段过滤、timeline end_ms 测试。
- 修改清理流程时，补结算告警归属和残余段 flush 测试。
- 修改 Gateway 配置时，补 relaxed/bypass/normal 三类路径测试。
- 修改 Lab 上传时，补单段失败不影响整请求的响应结构测试。

## 代码来源

- `tests/`
- `integration_tests/`
- `app/services/inference/`
- `app/services/persistence/`
- `app/routers/traceback.py`
- `app/routers/lab.py`

