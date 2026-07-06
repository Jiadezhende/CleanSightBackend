"""时序分析层 (L3/L4)。

抽象：Operator / AlignedFrame（operator.py，合并 analyze 推进状态 + judge 出告警）
处理流程：ClientTemporalActor 每 Client 1Hz tick（actor.py）→ 告警经 alarm_sink.persist_alarms
过闸 + 落库（alarm_sink.py 编排 client 侧过闸 + persistence 无状态落库；actor 只在产出处把
stage 别名烧进 alarm.stage）。

纯包标记，不做 re-export——消费方按需走深路径导入（抽象 `.operator`；管件
`.actor` 不对外平铺暴露）。
"""
