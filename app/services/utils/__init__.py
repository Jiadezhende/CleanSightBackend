"""services/utils —— 服务层工具：多个 service / router 都要、但不属于任何一个的纯函数。

    from app.services.utils.vod_playlist import VodEntry, render_vod

**它不是一个服务**：没有活体、没有单例、没有 `lifespan()`、不出句柄。所以「不建 service
对 service 的直接依赖」那条对它不适用——谁都可以向下依赖它，正如谁都可以向下依赖
`app/storage`。

## 边界

    可以 import   stdlib、三方、app.domain、app.storage、app.utils、app.settings
    不许 import   **任何兄弟 service 包**（app.services.lab / app.services.traceback / …）
                  app.routers、app.database / app.models
    不许有        活体、单例、lifespan()、模块级状态、类句柄

**「不许 import 兄弟 service」是本层存在的全部前提**。破了它，本包就成了 service → service
依赖的后门：`lab` 想调 `traceback` 的东西，只要在这里加个转发函数就绕过去了，而门禁与
review 都只会看到「一个工具包」。那比三份重复实现更坏——重复至少是看得见的。
这条由 `tests/test_import_hygiene.py` 的 `LAYER_PACKAGES` 守。

**与 `app/utils/` 的分工**：那边是全层通用**基建**（异常层次、GuardedExecutor、指标、
串行队列、网关中间件），与业务格式无关，连 `app/storage` 都可以在它下面。本包放的是
**服务层的业务格式与转换**（m3u8 文本、URI 装配之类），它认识本仓库的产物长什么样。
混在一起会让基建包失去主题：往 `app/utils` 里塞一个 m3u8 渲染器之后，下一个人塞什么
就没有判据了。

**准入判据**（沿用数据层那条）：这个知识有几个包会因为它变了而出错？**< 2 不进**——
留在那个唯一的主人那里。一个 service 自己用的纯函数就放它自己包里。

## 成员

    vod_playlist.py   VOD 形态 m3u8 的条目形状与文本渲染

本包是**标记型** `__init__.py`（规范 §3）：纯 docstring、零 re-export，消费方走深路径。
re-export 会让整棵子树的依赖变 eager，而这里将来可能进带重依赖的工具。
"""
