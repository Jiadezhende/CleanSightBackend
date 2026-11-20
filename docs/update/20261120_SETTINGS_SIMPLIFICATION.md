# 精简部署配置并收回网关及 Lab 内部策略

> **变更状态**：生效中（2026-11-20）
> **知识库**：已沉淀 → [SERVICE_SETTINGS.md](../kb/SERVICE_SETTINGS.md)、[SERVICE_CONFIG.md](../kb/SERVICE_CONFIG.md)（2026-11-20）

## 概述

HTTP 路径分类与 Lab 导出策略收回所属模块，Settings 从 54 个实际模型字段减至 48 个。模板只展示常规部署信息，完整说明归档到 docs/kb/SERVICE_SETTINGS.md。未使用自定义内部策略的部署行为保持原样。

## 变更背景

路径分类取决于路由的轮询频率、业务响应和鉴权方式，应随代码更新。Lab 导出编码与提交上限仅属于模块策略。逐个部署目录维护这些策略增加运行配置负担；跨模块参数与现场资源信息仍需保留集中定义。

## 方案详情

请求进入网关 → 使用固定路径规则选择普通／宽松／绕过档 → 按 Settings 中的配额执行检查。

| 部件 | 变更 |
|---|---|
| app/gateway.py | 定义 _RELAXED_PREFIXES、_BYPASS_PREFIXES，直接使用元组，删除实例内路径副本和字符串拆分 |
| app/settings.py | 删除 gateway_relaxed_prefixes、gateway_bypass_prefixes；保留 gateway_relaxed_rate_limit |
| config/settings.example.yaml | 删除路径设置入口，说明路径由代码维护，修正模板首行文件名 |
| tests/test_gateway.py | 不再注入路径字段，使用实际代码规则验证分档行为 |

Lab 的 preset、单段／总时长与数量上限移入 service.py 内部常量，沿用 veryfast、300000ms、1800000ms、20。submit_limits、提交校验与 ClipBuilder 共用这些值；迁移已有测试，继续验证参数传递与接口上限一致。Label Studio 连接信息、凭据和临时目录仍属于部署配置。

帧率、缓存、容量规划及渲染参数仍保留 Settings 单一真源。工具路径、网关限额和其他调优项不在常规模板中展开，完整默认值及使用时机归档到 docs/kb/SERVICE_SETTINGS.md；README 增加入口，不新增配置文件或解析层。原 docs/SETTINGS.md 保留迁移链接。

原列表完整迁入，不改变 startswith 匹配、绕过优先级、限流和封禁行为。宽松档不参与升级封禁和反扫描；绕过档跳过限流及反扫描，两者均保留 IP 白名单与封禁检查。/media 的 HMAC 鉴权仍由路由负责。

## 迁移

已有 config/settings.yaml 如果包含 gateway_relaxed_prefixes 或 gateway_bypass_prefixes，升级前需要删除，否则未知字段检查会拒绝加载。旧 CLEANSIGHT_GATEWAY_RELAXED_PREFIXES 和 CLEANSIGHT_GATEWAY_BYPASS_PREFIXES 不再生效。实际运行配置未读取或修改。

同样删除 lab_export_ffmpeg_preset、lab_export_max_clip_ms、lab_export_max_total_ms、lab_export_max_clips_per_submit；相应旧环境变量不再生效。曾使用自定义策略的部署需先核对差异，不能假定仍沿用旧值。

## 变更效果与验证

网关回归使用真实路径策略，覆盖宽松限流、/admin/ 与探测路径的区别、媒体绕过、白名单及封禁。全量单元／组件测试 1276 项通过，包含配置、Lab 与 daemon 回归；实际运行配置及旧环境文件被屏蔽，业务连接使用不可达占位值。模板所有键均属于 Settings，所有保留字段均在模板或高级说明中覆盖，git diff --check 通过。

## 遗留风险 / 后续任务

未来修改路径分类或 Lab 策略需同步代码及行为测试。现有前缀匹配语义完整保留，本轮不调整匹配边界。未进行实际配置迁移或真实服务启动验收。

补全文档时核验了现有 env_ignore_empty 限制：YAML／进程变量的空 cuda_device、mediamtx_bin 分别回退到 0、auto，不能按下游空值约定选择 CPU／纯代理。完整说明记录了该限制，模板删除错误提示；本次未改加载行为，需后续单独修正和回归。
