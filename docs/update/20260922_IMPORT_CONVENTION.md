# 导入规范统一：包内相对、跨包绝对，并补上让两条老门禁失效的相对导入盲区

> **变更状态**：生效中（2026-09-22）——76 处改写，全量 `pytest` 827 passed，新门禁做过注入验证（三类违规各能红）
> **知识库**：待沉淀

## 概述

`app/` 与 `mediamtx_gateway/` 的 import 统一成 **包内一律相对（`from .x` / `from .sub.x`）、跨包一律绝对（`from app....`）、相对不上翻**，共改写 76 处。规则写进 [DEVELOPMENT.md §8](../DEVELOPMENT.md)，由 `tests/test_import_hygiene.py` 新增的第三条门禁执行。

同时修掉一个既有缺陷：**该文件原有的两条 AST 门禁看不见相对导入**，跨包依赖只要写成相对形式就能绕过。已加 `_abs_module()` 统一还原成绝对再判。

## 变更背景

### 原状是按代码新旧分裂的，不是按规则

扫 `app/` 全部 `ImportFrom`：34 处相对、206 处绝对，其中 55 处绝对指向自己所在的包。新写的包（`storage/hls` 12、`services/client` 4、`services/lab` 3）自发用相对，老包（`inference` 14、`health_monitor` 7、`app/` 根 7）全用绝对，`stream/manager.py` 与 `utils/executor.py` 两个文件内部就混着两种写法。

### 真正的动因是门禁有洞

`test_import_hygiene.py` 的两条 AST 门禁按**模块名字符串**判定依赖：

- `test_singleton_reference_surface`：`node.module == "app.services.inference.instance"`
- `test_layer_package_imports_only_whitelisted_app_modules`：`name.startswith("app.")` 才检查，注释写着「相对 import（level > 0）是包内寻址，天然合规」

这个假设只在「相对导入必然是包内」时成立，而当时并无任何东西保证这一点。后果：把一条跨包依赖写成 `from ..client.manager import client_manager`，两条门禁都不报。统一写法与补门禁必须一起做——只做前者，规则会随时间漂回去；只做后者，两种合法写法并存仍是绕过面。

## 方案详情

### 规则（DEVELOPMENT.md §8 正文）

```python
# app/services/inference/manager.py
from .config import load_stage_config                     # ✓ 同目录
from .detection.service import DetectionService           # ✓ 本包子包
from app.services.client.manager import client_manager    # ✓ 跨包
from app.services.inference.naming import stream_name     # ✗ 包内却写绝对
from ..client.manager import client_manager               # ✗ 上翻
```

判据是「目标是不是我这个包的后代」，与目录深浅无关。收益不止一致性：**`from app.` 开头的行从此等价于「这是外部依赖」**，跨服务依赖（规范 §3 重点盯的东西）在 import 段一眼可数。

### 改写范围

| 轮次 | 内容 | 处数 | 文件数 |
|------|------|------|--------|
| 1 | 同目录：`from app.services.x.config` → `from .config` | 56 | 33 |
| 2 | 本包子包：`from app.services.inference.detection.service` → `from .detection.service` | 20 | 5 |

改写用 AST 定位 + 按行正则替换（只动 `from <module> import` 的模块段），不碰缩进与函数体内 import 的位置。改完实测：相对 110 处全为 level 1，绝对 454 处全为跨包，零残留。

### 门禁

新增 `_abs_module(path, node)`：把任意 `ImportFrom` 按文件位置还原成绝对模块名（`__init__.py` 的末段就是 `__init__`，与普通模块同一式子成立）。三条老检查全部改用它：

- `test_singleton_reference_surface` —— 否则 `from .instance import inference_manager` 隐身；
- `test_layer_package_imports_only_whitelisted_app_modules` —— 白名单本就含本包（`"app.storage"`），还原后包内寻址照样放行，但写成相对不再能绕过白名单；
- `test_services_do_not_import_routers` —— 同理。

新增 `test_intra_package_relative_cross_package_absolute`，三类违规分开断言、各自给改法：上翻、包内写绝对、跨包写相对。

注入验证：往 `app/services/lab/clip_builder.py` 塞三类违规各一条，门禁如期红；已还原。

## 影响面

| 面 | 变化 |
|----|------|
| 运行时 | 无。模块身份不变，`sys.modules` 键不变，测试里 `monkeypatch.setattr("app.services.x.y", ...)` 这类字符串目标照常生效 |
| `stage_factory._import_class` | 不受影响，它走 `importlib.import_module(dotted_path)`，YAML 里配的类路径仍是绝对 |
| 门禁强度 | 净增：两条老检查补上相对导入盲区，再加一条写法锁定 |
| 测试 | 827 passed（`test_import_hygiene.py` 32 passed） |

## 遗留

- `docs/update/20260903_PACKAGE_LAYOUT_SPEC.md` 是本规则的上游推导，其中「相对 import 天然合规」的表述已被本次推翻，融合 KB 时以 DEVELOPMENT.md §8 为准。
