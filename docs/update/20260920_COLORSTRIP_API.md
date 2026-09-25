# 试纸比色开放 HTTP 接口 + 新建 `app/algorithm/` 算法层

> **变更状态**：已实现（2026-09-20）
> **知识库**：待沉淀
> **前置**：无。算法本身的可行性验证、判据标定与已知缺陷见
> `app/services/temp/colorstrip/REPORT.md`（含 28 MB 样本，整个目录在 `.gitignore` 里）

## 做了什么

给过氧乙酸试纸色卡比色算法开了 HTTP 接口 `POST /algorithm/colorstrip`：base64 图片进，
合格/不合格出，判不出来时给结构化失败码与该怎么补拍。

顺带解决了一个更要紧的问题：**算法此前住在 `app/services/temp/colorstrip/`，而
`temp/` 整个在 `.gitignore:168` 里——代码根本不在仓库**。接口一旦依赖它，本机能跑、
服务器上 ImportError。本次把运行时那部分搬进仓库，独立成 `app/algorithm/` 顶层包。

## 1. 新建 `app/algorithm/` 算法层：无状态纯计算，零 `app.*` 依赖

```
app/algorithm/
  __init__.py                 标记型：纯 docstring，零 re-export
  colorstrip/
    __init__.py               标记型
    params.yaml               全部阈值 + 入参上限 + 默认档名（单一真源）
    types.py                  Params / 结果码 / 结果码→操作员提示
    config.py                 读同目录 params.yaml，档位解析
    grader.py                 判定算法
    cli.py                    python -m app.algorithm.colorstrip.cli 图片...
```

**为什么是顶层包而不是塞进某个 service**：算法无活体、无状态、谁都可以向下依赖它。
放进 `app/services/lab/` 之类，别的 router 要用就成了规范 §3 禁止的 service → service 依赖。

**自包含到什么程度**：连"图多大算超限""默认用哪一档"都写在 `params.yaml` 里，
**不进 `app/settings.py`、不进顶层 `config/`**。这条由门禁把住——
`LAYER_PACKAGES` 里给 `app/algorithm` 的白名单是 `("app.algorithm",)`，
谁从这里伸手去够 `app.settings` 或任何 service，`test_layer_package_imports_only_whitelisted_app_modules`
直接红。算法只抛 `ValueError` / `KeyError`，翻成 HTTP 是 `routers/` 的活。

> 这一条偏离规范 §8（"运维要改的 yaml 放顶层 `config/`，好整目录覆盖或挂载"）。
> 已决策：各算法互不相干，不需要集中调参，自包含换来的是"一个算法包能整个拷走、单独跑"。
> 代价是现场要按点位调参时得改包内文件而非挂载覆盖——真遇到了再往外挪，只动 `config.py` 一行。

**搬运时改的三处**（不是照抄）：

| 改动 | 为什么 |
|---|---|
| `import cv2` 从模块级挪进 `segment` / `draw` / `imwrite` / `imdecode` 函数体 | cv2 是 L2 依赖，规范 §2 禁止模块顶层 import。`routers/algorithm.py` 模块级 import 到 `grader`，顶层拽 cv2 会让 `app.main` 的"零重依赖"预算失守 |
| `Params` 从 `config.py` 挪到 `types.py` | 规范 §1：文件名即依赖上界，`config.py` 只管读 YAML |
| 新增 `imdecode(bytes)`，`imread` 改为走它 | HTTP 收到的是内存字节，不是路径。顺带保留了绕开 `cv2.imread` 在非 ASCII 路径上返回 None 的那层（本仓库路径含中文） |

## 2. 验收工装留在 `temp/`，改成引用新位置

`stats.py` / `testset.py` / `acceptance.py` / `golden.json` / `samples/`（28 MB）/ `REPORT.md`
仍在 `app/services/temp/colorstrip/`——样本太大不该入库。

新增 `_bootstrap.py` 把仓库根挂上 `sys.path`，三个脚本的 `import grader` 改为
`from app.algorithm.colorstrip import grader`，**工作流不变**：仍是
`cd app/services/temp/colorstrip && python acceptance.py`。

搬完后门禁 **72/72 原样绿**（40 姿态不变性 + 32 对抗用例），证明搬家没改行为。

## 3. 新路由 `app/routers/algorithm.py`

单开一组 `/algorithm/*`，**没有挂进 `/lab-f3m8`**：lab 那组是送标数据飞轮（圈选区间 →
剪 mp4 → 传 Label Studio），有任务、有产物、有外部依赖；比色只是把一张图喂给算法。
两者除了"都给操作员用"之外没有共同点。

契约全文见 [docs/api/algorithm.md](../api/algorithm.md)。三条定下来的口径：

**① 请求一个字段，响应四个平铺字段。**

```jsonc
// 请求
{ "image_base64": "..." }           // 裸 base64 与 data URL 都收

// 响应恒 200
{ "ok": true,  "passed": true,  "code": "OK",        "message": "合格：试纸比参考色深" }
{ "ok": false, "passed": null,  "code": "E_NO_CARD", "message": "未找到色卡：完整色卡两块都要入镜..." }
```

测量值（L\*、裕度、色相）、色块坐标、标注效果图、判定日志**都不进响应**。
拒判时带实测值的判据诊断（`深块色相 48.5°∉[36,56]`）是调参用的，`logger.info` 进服务端日志；
调参走 CLI。以后前端真要展示裕度，加字段是向后兼容的。

**② 算法拒判是 200 + `ok:false`，不是 400。**
没拍到色卡、试纸数不对属于"照片拍得不对"，重发同样的请求没意义、让用户重拍才有意义。
400 只留给请求层问题：base64 解不开、图超过 12 MB、`?profile=` 写错。

`ok` 与 `passed` 刻意是两个字段：**判不出来不等于不合格**，
前端写 `if (!resp.passed)` 会把 `null` 当假值，于是把"拍糊了"显示成"不合格"。

**③ 端点写成同步 `def`，不是 `async def` + `run_in_threadpool`。**
实测一张手机照全是 CPU 阻塞：

| 样本 | 大小 | 解码 | 判定 | 合计 |
|---|---|---|---|---|
| case4（1279×1706） | 0.5 MB | 8 ms | 17 ms | 25 ms |
| case2（3468×4624） | 4.6 MB | 55 ms | 23 ms | 77 ms |
| case0（两条试纸） | 4.1 MB | 54 ms | 102 ms | **156 ms** |

留在事件循环上会把同进程的 `/ai/video` 推理画面 WS 一起钉住。FastAPI 对同步 `def` 端点
自动丢进 AnyIO 的进程级工作线程池，与 `run_in_threadpool` 是**同一个**池子
（没有谁"启动线程池"，uvicorn 起进程时就建好了），但少一个 import、少一层闭包。
本端点整个函数体都是阻塞活，没有需要留在循环上的部分。

尺寸上限**先量 base64 字符串长度再解码**（base64 膨胀 4/3）——为了拒绝一张超大图而先把它
整个吃进内存是本末倒置。

## 4. 门禁与测试

- `tests/test_import_hygiene.py`：`LAYER_PACKAGES` 加 `app/algorithm`（白名单只有它自己）；
  `BUDGET` 逐模块登记 6 条，重依赖集合全为 `set()`。以后谁把 cv2 挪回模块级会直接红。
- `tests/test_algorithm_router.py`（新，14 例）：**用 numpy 合成小图**，色值按 REPORT §2.5 的
  实测区间取，不依赖 28 MB 样本。覆盖合格/不合格、三种拒判码走 200、五种请求层问题走 400、
  data URL 前缀、`?profile=` 选档与写错。

全量 849 通过。

## 影响面

- 新增：`app/algorithm/**`、`app/routers/algorithm.py`、`tests/test_algorithm_router.py`、
  `docs/api/algorithm.md`
- 修改：`app/main.py`（注册路由）、`tests/test_import_hygiene.py`（登记新层）、
  `docs/api/README.md`（索引）
- **`app/settings.py` 没动**——上限与默认档在算法包自己的 `params.yaml` 里
- 不影响任何既有端点；本端点不读库、不写盘、不发告警

## 已知缺口（承自算法本身，接口不改变这些）

1. **临界区没有真实样本**。裕度很小时结论不可信。定位是防误操作与留痕，不是精密定量。
2. **不防蓄意伪造**。画面里若完全没有真色卡、却有两块红橙物凑齐色卡的结构与色值，
   算法会把它当色卡并给出判定（REPORT §6.1 的 A11b 是实测的误放行）。
   防造假该由时间戳、随机水印、现场抽查承担。
3. **色相窗口只在 8 张光照条件相近的样本上标定**，跨色温未验证，强色温偏移下可能误拒
   （方向安全但影响可用性）。`params.yaml` 里备了 `warm_light` / `low_res` 两个**未标定**的
   示例档，只是给换场景时照抄的模板，注释里写明了别直接用。
