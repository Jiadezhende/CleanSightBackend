> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Algorithm Service（试纸比色）

`app/services/algorithm/` 是无状态纯计算服务：图片字节进、结论出。当前只有过氧乙酸试纸色卡比色（`colorstrip`）一个算法；判定标准的业务口径见 [BUSINESS_DETECTION_STANDARDS.md](BUSINESS_DETECTION_STANDARDS.md)。

## 与视频巡检主流程完全解耦

- 不关联 task / step，不读库、不写盘、不发告警；唯一调用方是 `app/routers/algorithm.py`。
- 无单例、无 `instance.py`、无 `lifespan()`，`app/main.py` 只 `include_router(algorithm.router)`。

## 调用链：router 只把两个具名异常翻成 400

```text
POST /algorithm/colorstrip[?profile=]            routers/algorithm.py（同步 def）
  │ _decode_image_base64：裸 base64 / data URL → bytes
  │   解码前按 base64 长度 ×3/4 估算，> colorstrip_max_image_bytes() 即 400；解码后再核一次
  ▼
service.grade_colorstrip(bytes, profile)          services/algorithm/service.py
  ├─ colorstrip.config.load(profile)    KeyError   → UnknownProfileError
  ├─ colorstrip.grader.imdecode(bytes)  ValueError → ImageDecodeError
  └─ colorstrip.grader.grade(img, cfg)  → dict(ok, code, strips, log, …)
  ▼
ColorstripVerdict(ok, passed, code, message)      拒判时 log 只进服务端 logger.info
  ▼
router：UnknownProfileError → ValidationError(field="profile")        → 400
        ImageDecodeError    → ValidationError(field="image_base64")   → 400
```

router 不宽接 `ValueError` / `KeyError`，否则算法内部 bug 会被报成 400 而不是 500。请求 / 响应 schema 与错误码见 [docs/api/algorithm.md](../api/algorithm.md)。

## 包结构：一个算法一个自包含子包

```text
app/services/algorithm/
  __init__.py          标记型，零 re-export
  service.py           对外接口（模块级函数）：grade_colorstrip / colorstrip_max_image_bytes /
                       ColorstripVerdict / ImageDecodeError(ValueError) / UnknownProfileError(KeyError)
  colorstrip/
    __init__.py        标记型
    params.yaml        全部阈值 + 入参上限 + 默认档名（单一真源，代码里无默认值副本）
    types.py           Params、5 个结果码、CODE_HINTS、message_for（stdlib only）
    config.py          读同目录 params.yaml，base + profiles 两层深合并
    grader.py          分割 / 认色卡 / 判定 / 效果图
    cli.py             调参入口，不被包内任何模块 import
```

- cv2 只在 `grader.py` 的函数体内 import，模块级只有 numpy：本包经 `service` 被 router 模块级 import，cv2 挪到顶层会让 `app.main` 的导入预算失守。
- `config` 的读取带 `functools.lru_cache`，params.yaml 每进程只读一次，改文件须重启后端。

## 零 app.* 依赖，由导入门禁强制

- 整个 `app/services/algorithm` 不许 import 任何 `app.*`（含 `app.settings`），目的是算法子包能整个拷走单独跑。由 `tests/test_import_hygiene.py` 的 `LAYER_PACKAGES` 强制；7 个模块都在 `BUDGET` 逐个登记，重依赖集合为空。
- 翻成 HTTP 错误是 router 的事，服务只抛自己的具名异常。分层门禁全貌见 [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)。

## 配置在包内 params.yaml，不进 config/ 与 settings

- `app/services/algorithm/colorstrip/params.yaml`：`default_profile: default`、`max_image_bytes: 12000000`（base64 解码后的字节上限）、`base`（`segment` / `card_pair` / `color_window` / `spec` 四组判据）、`profiles`（每档只写覆盖项）。
- 这是「运维 yaml 集中在 `config/`」的例外，由零 `app.*` 依赖决定，现场调参须改包内文件、不能挂载覆盖。
- `default` 是验收门禁基线；`warm_light`（色相窗口下推 8°）与 `low_res`（放宽分割）是未经样本标定的示例档。新场景新开一档，不改 `default`。
- 档名不存在 → `KeyError`，不回退；params.yaml 缺失 → `FileNotFoundError`。

## 判定：同图相对比色，试纸比 2000 块深即合格

- 色卡（800 / 2000 两个刻度块）必须与试纸同框：同帧共享光照与白平衡。
- `grader.grade` 流程：长边归一化到 `target_long` → HSV 红橙色相带 + 饱和度 / 明度过滤 → 形态学 → 连通域出色块 → 按结构（沿主轴紧邻、垂直对齐、等大、ΔL\* 区间）与 Lab 色相窗口认出唯一一对色卡（深块 = 2000）→ 其余色块必须恰好 `spec.expected_strips`（=1）条 → **试纸 L\* < 2000 块 L\* 即合格**。色相轴只写日志，不参与结论；相对位置与整图旋转不影响判定。
- 结果码（`colorstrip/types.py`）：`OK`、`E_TOO_FEW_PATCHES`（色块 < 2）、`E_NO_CARD`（无合法色卡对）、`E_CARD_AMBIGUOUS`（多组候选）、`E_STRIP_COUNT`（待测色块数 ≠ 规范值）；拒判时 `message_for` 给补拍建议。
- **判不出来 ≠ 不合格**：拒判返回 200 + `ok=false` + `passed=None`；400 只用于请求层问题（空串、非法 base64、超上限、非图片字节、档名错）。判据诊断只写服务端日志。
- 调参：`python -m app.services.algorithm.colorstrip.cli 图.jpg [-p 档名] [--viz 输出目录]`。

## 接入：同步 def 端点，Gateway 走普通配额

- 端点是同步 `def`，FastAPI 放线程池跑：函数体全是 CPU 阻塞活，放事件循环上会卡住同进程的 `/ai/video` WS。耗时量级（4.6 MB 手机照约 78 ms，最慢 156 ms）出自代码注释里的一次实测，无可复现基准，待核验。
- `/algorithm` 不在宽松 / 绕过前缀里，按普通配额（60 次 / 60s、超限升级封禁、404/405 计入反扫描），见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。

## 测试：仓库内只测接线，判据回归在仓库外工装

- `tests/test_algorithm_router.py` 用合成小图测路由接线。
- 判据回归在 `ref/colorstrip/`（整个 `ref/` 被 `.gitignore` 忽略，含约 28 MB 真实样本）：`acceptance.py` 是门禁，基线 `golden.json` 记录冻结时的参数，改 `default` 档后须 `python acceptance.py --freeze` 重冻结，否则报「基线参数与当前参数不符」；`stats.py` 看新样本的颜色区间与余量。

## 已知缺口：防误操作，不防伪造

- **临界区无真实样本**：试纸与 2000 块 L\* 接近时结论不可信。
- **不防蓄意伪造**：色相窗口只用来认块；照真色卡取色的伪造照样放行。
- **跨色温未验证**：`default` 的色相窗口只在 8 张光照相近的样本上标定，换光源须新开档并用新样本标定。
- 量化依据在工装的 `REPORT.md`（不入库），待核验。

## 新增一个算法

1. 在 `app/services/algorithm/` 下建自包含子包（自带 `params.yaml` / `types.py` / `config.py` / 实现 / 可选 `cli.py`，标记型 `__init__`，重依赖在函数体内 import）。
2. 在 `service.py` 加模块级接口函数与具名异常（`ValueError` / `KeyError` 子类）。
3. 在 `tests/test_import_hygiene.py` 的 `BUDGET` 逐模块登记。
4. 在 `routers/algorithm.py` 加端点（重 CPU 用同步 `def`），只接具名异常翻 400；契约写进 `docs/api/algorithm.md`。

## 代码来源

- `app/services/algorithm/{__init__,service}.py`
- `app/services/algorithm/colorstrip/{params.yaml,types.py,config.py,grader.py,cli.py}`
- `app/routers/algorithm.py`、`app/main.py`、`app/settings.py`（gateway 前缀）
- `tests/test_algorithm_router.py`、`tests/test_import_hygiene.py`
