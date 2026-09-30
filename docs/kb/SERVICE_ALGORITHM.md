> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Algorithm Service（试纸比色）

`app/services/algorithm/` 是**无状态纯计算**服务：图（原始字节）进、结论出。当前只有一个算法——过氧乙酸试纸色卡比色（`colorstrip`）。

## 定位与边界

- 与内镜视频巡检主流程（推理 / 录制 / 告警）无关：不关联 task / step，不读库、不写盘、不发告警。唯一调用方是 `app/routers/algorithm.py`。依据：`app/services/algorithm/__init__.py`、`service.py` 模块 docstring。
- **无活体、无单例、无 `lifespan()`**：没有 `instance.py`，不进 `app.main` 的 lifespan 启动序列（`app/main.py` 的 lifespan 链里没有 algorithm，只 `include_router(algorithm.router)`）。
- 定位是防误操作与留痕，不是精密定量，也不防蓄意伪造（见「已知缺口」）。

## 调用链

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

- router 只接这两个具名异常，**不宽接 `ValueError` / `KeyError`**——否则算法内部 bug 会被报成 400 而不是 500。依据：`service.py` docstring、`routers/algorithm.py::grade_colorstrip`。
- 请求 / 响应 schema、错误码、拍照要求不进 KB，见 [docs/api/algorithm.md](../api/algorithm.md)。

## 包结构

```text
app/services/algorithm/
  __init__.py          标记型：纯 docstring、零 re-export
  service.py           对外接口（模块级函数即接口）：grade_colorstrip / colorstrip_max_image_bytes /
                       ColorstripVerdict / ImageDecodeError(ValueError) / UnknownProfileError(KeyError)
  colorstrip/          一个算法一个自包含子包
    __init__.py        标记型
    params.yaml        全部阈值 + 入参上限 + 默认档名（单一真源，代码里无默认值副本）
    types.py           Params、5 个结果码、CODE_HINTS、message_for（stdlib only）
    config.py          读同目录 params.yaml，base + profiles 两层深合并
    grader.py          分割 / 认色卡 / 判定 / 效果图
    cli.py             调参入口（单向出口，不被包内任何模块 import）
```

- cv2 只在 `grader.py` 的函数体内 import（`imdecode` / `imwrite` / `segment` / `draw`）；模块级只有 numpy。本包经 `service` 被 `routers/algorithm.py` 模块级 import，cv2 挪回顶层会让 `app.main` 的导入预算失守。
- `config._doc` / `config.load` 带 `functools.lru_cache`：params.yaml 每进程只读一次，改文件要重启后端才生效。

## 依赖硬约束

- 整个 `app/services/algorithm` **零 `app.*` 依赖**（连 `app.settings` 都不许）：`tests/test_import_hygiene.py` 的 `LAYER_PACKAGES["app/services/algorithm"] = ("app.services.algorithm",)`。目标是一个算法子包能整个拷走单独跑。
- 7 个模块在 `BUDGET` 逐模块登记，重依赖集合都为空（`test_layer_package_modules_are_all_budgeted` 强制）。
- 翻成 HTTP（`ValidationError`）是 router 的活，服务只抛自己的具名异常。

分层门禁全貌见 [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)。

## 配置

- 单一真源 `app/services/algorithm/colorstrip/params.yaml`：`default_profile: default`、`max_image_bytes: 12000000`（base64 **解码后**原始字节上限）、`base`（`segment` / `card_pair` / `color_window` / `spec` 四组判据参数）、`profiles`（每档只写覆盖项，按两层深合并）。
- **刻意不进 `app/settings.py`、不进顶层 `config/`、不读 `settings.config_dir`**——这是「运维 yaml 集中在 `config/`」的例外，由零 `app.*` 依赖决定；代价是现场调参要改包内文件，不能挂载覆盖。
- 档位：`default` 是验收门禁的基线；`warm_light`（色相窗口整体下推 8°）与 `low_res`（放宽分割）是**未标定**的示例档（params.yaml 注释明写「没有样本标定过」）。新场景应新开一档，不改 default。
- 档名不存在 → `KeyError`（不回退）；params.yaml 缺失 → `FileNotFoundError`（fail-fast）。

## 判定语义

- **相对比色**：同一张图里同时拍到瓶身参考色卡（800 / 2000 两个刻度块）与待测试纸；同帧共享光照与白平衡，所以色卡必须与试纸同框。
- 流程（`grader.grade`）：长边归一化到 `target_long` → HSV 红橙色相带 + 饱和度 / 明度过滤 → 形态学 → 连通域出色块 → 两两配对按结构（沿主轴紧邻、垂直对齐、等大、ΔL* 区间）与 Lab 色相窗口认出唯一一对色卡（深块 = 2000 = 参考下限）→ 其余色块必须恰好 `spec.expected_strips`（=1）条 → **试纸 L\* < 2000 块 L\* 即合格**。色相轴只作并行参考写进日志，不参与结论。色卡与试纸的相对位置、整图旋转不影响判定。
- 5 个结果码（`colorstrip/types.py`）：`OK`、`E_TOO_FEW_PATCHES`（色块 < 2）、`E_NO_CARD`（找不到合法色卡对）、`E_CARD_AMBIGUOUS`（多组候选色卡）、`E_STRIP_COUNT`（待测色块数 ≠ 规范值）。`message_for` 给操作员一句话：OK 时说合格与否，拒判时给补拍建议（`CODE_HINTS`）。
- `ok` 与 `passed` 分开：拒判是 200 + `ok=false` + `passed=None`，**判不出来 ≠ 不合格**；400 只用于请求层问题（空串、非法 base64、超上限、非图片字节、档名写错）。带实测值的判据诊断（最接近的候选对卡在哪条判据）只写服务端 `logger.info`，不进响应。
- 调参入口：`python -m app.services.algorithm.colorstrip.cli 图.jpg [-p 档名] [--viz 输出目录]`，打出每条判据的实测值，可落效果图。

## 并发

- 端点是**同步 `def`**，FastAPI 放进 AnyIO 线程池执行：整个函数体都是 CPU 阻塞活，放在事件循环上会卡住同进程的 `/ai/video` WS。依据：`routers/algorithm.py` 模块 docstring。
- 耗时量级：代码注释记为 4.6 MB 手机照约 78 ms（解码 55 ms + 判定 23 ms）、最慢样本 156 ms——**待核验**：出自一次实测，仓库内没有可复现的基准。

## Gateway 分档

`/algorithm` 不在 `gateway_relaxed_prefixes` / `gateway_bypass_prefixes` 里，走 **normal** 档（60 次/窗、超限升级封禁、404/405 计入反扫描）。依据：`app/settings.py` 的 gateway 前缀配置；分档机制见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。

## 测试与验收工装

- 仓库内：`tests/test_algorithm_router.py` 用 numpy + cv2 合成小图测路由接线（合格 / 不合格、拒判 200、请求层 400、data URL、档名）；只管「路由接得对不对」。导入约束由 `tests/test_import_hygiene.py` 把住。
- 算法判据本身的回归在验收工装 `ref/colorstrip/`：位于仓库根下但整个 `ref/` 被 `.gitignore` 忽略、不入库（含约 28 MB 真实样本）。`acceptance.py` 是门禁（结论一致 / 分割稳定 / 安全方向 / 姿态一致四条判据，基线存 `golden.json` 并记录冻结时的参数）；改 default 档后要 `python acceptance.py --freeze` 重冻结，否则门禁报「基线参数与当前参数不符」；`stats.py` 看新样本的颜色区间与余量；`_bootstrap.py` 把仓库根挂上 sys.path，工装直接 `from app.services.algorithm.colorstrip import …`。工装与后端运行时无任何耦合。

## 已知缺口

算法本身的局限，接口形态不改变它们：

- **临界区没有真实样本**：试纸与参考块 L\* 接近时结论不可信（`grader.grade` 注释：两条轴都还没有临界真样本可验证）。
- **不防蓄意伪造**：色相窗口只用来认「谁是 800、谁是 2000」，不是防伪手段；照着真色卡取色的伪造照样放行，职责边界已划定为「防误操作、不防蓄意伪造」。依据：`grader.py` 模块 docstring、`params.yaml` 的 `color_window` 注释。
- **跨色温未验证**：default 档的色相窗口只在 8 张光照相近的样本上标定过（`params.yaml` 注释），换光源需新开档并用新样本标定。
- 上述三条的量化依据在工装的 `REPORT.md`（不入库）——**待核验**。

## 新增一个算法

1. 在 `app/services/algorithm/` 下新建自包含子包（自带 `params.yaml` / `types.py` / `config.py` / 实现 / 可选 `cli.py`，标记型 `__init__`，重依赖函数体内 import）。
2. 在 `service.py` 加模块级接口函数与该算法的具名异常（`ValueError` / `KeyError` 子类）。
3. 在 `tests/test_import_hygiene.py` 的 `BUDGET` 逐模块登记新文件。
4. 在 `routers/algorithm.py` 加端点（重 CPU 用同步 `def`），只接具名异常翻 400；契约写进 `docs/api/algorithm.md`。

## 代码来源

- `app/services/algorithm/__init__.py`、`app/services/algorithm/service.py`
- `app/services/algorithm/colorstrip/{params.yaml,types.py,config.py,grader.py,cli.py}`
- `app/routers/algorithm.py`、`app/main.py`（`include_router(algorithm.router)`）
- `app/settings.py`（gateway 前缀）
- `tests/test_algorithm_router.py`、`tests/test_import_hygiene.py`
