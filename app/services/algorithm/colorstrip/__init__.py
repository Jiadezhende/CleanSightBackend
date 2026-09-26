"""过氧乙酸试纸色卡比色判定：以色卡 2000 刻度块为参考下限，试纸更深即合格。

  from app.services.algorithm.colorstrip import config, grader
  res = grader.grade(grader.imdecode(raw_bytes), cfg=config.load())

- params.yaml  全部阈值 + 入参上限 + 默认档名（单一真源，代码里无默认值副本）
- config.py / types.py / grader.py / cli.py

标记型 `__init__`（规范 §3）：零 re-export，消费方走深路径——re-export 会让整棵子树的
重依赖变 eager，而 cv2 是禁止模块顶层 import 的 L2 依赖。

设计推导、判据实测依据与已知缺陷见 `docs/update/20260920_COLORSTRIP_API.md` 与验收工装的
REPORT.md（`app/services/temp/colorstrip/`，含 28 MB 样本，不在仓库里）。
"""
