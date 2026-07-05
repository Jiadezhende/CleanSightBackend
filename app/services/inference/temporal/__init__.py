"""时序分析层 (L3/L4)。

抽象：Operator / AlignedFrame（operator.py，合并 analyze 推进状态 + judge 出告警）
处理流程：ClientTemporalActor 每 Client 1Hz tick（actor.py）→ 告警经 PersistenceManager.persist_alarms 落库
（告警落库属持久化领域，sink 已迁 persistence；actor 只在产出处把 stage 别名烧进 alarm.stage）。

纯包标记，不做 re-export——消费方按需走深路径导入（抽象 `.operator`；管件
`.actor` 不对外平铺暴露）。
"""
