"""离线分割策略实现集 —— 一个策略一个自包含包 ``<业务>_<模型>/``（模型名架构在前、特征在后）。

    <pkg>/__init__.py   来源与改动点      <pkg>/contract.py   随权重固定的契约（类序、特征、结构、物料文件名）
    <pkg>/segmenter.py  适配层，包内唯一 import 框架的文件；docstring 带 YAML 片段

硬约束（tests/test_import_hygiene.py 执行）：策略只许 import 自身与
``offline.segmentation.segmenter`` / ``inference.resample`` / ``app.types``；策略之间零 import，要用就复制一份。
StageFactory 按 YAML ``offline.class`` 全限定路径按需 import，本包初始化不 import 任何策略（避免拉起 torch）。
"""

__all__: list[str] = []
