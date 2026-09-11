"""services 包标记。

**刻意不 re-export 任何东西**（规范 §3 的「标记型」形态）：这里曾模块级
`from .client import client_manager`，零消费方，却让**每一个** `app.services.*` 的导入
都先付 282ms 与 455 个模块（client → numpy 链）的过路费——包括本该是 stdlib-only 的
`app.storage`。

导入包内任一子模块都会先执行本文件，所以写在这里的一切是包里每个消费者的固定成本。
消费方一律走深路径（`from app.services.client.instance import client_manager`）。
"""
