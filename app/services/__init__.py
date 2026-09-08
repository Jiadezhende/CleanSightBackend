"""app.services —— 标记型包，**刻意不 re-export 任何东西**。

此前这里有一句 `from .client import client_manager`（外加 `except ImportError:
client_manager = None` 兜底），**零消费方**，却让每个 `app.services.*` 子模块的 import
都付一笔过路费：空跑 `import app.services` 就要 0.303s（删后 0.002s），子包的耗时几乎全是
这一笔。那个 `except` 还会把真实的 ImportError（打错名字、少装依赖）静默换成 `None`，
表现为「某个 client 突然是 None」而不是 import 就炸。

消费方一律走深路径（`from app.services.client import client_manager`，删除前全仓已无一处
走此处的短路径），见[包结构规范](../../docs/update/20260903_PACKAGE_LAYOUT_SPEC.md)
§3/§5：包 `__init__` 只做标记，不做转发。
"""
