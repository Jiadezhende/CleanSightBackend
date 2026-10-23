"""离线动作分割 —— detections.jsonl → `OfflineSegmenter` 策略 → temporal.jsonl + label_probs.npz。

    segmenter.py   策略基类 OfflineSegmenter / OfflineContext（策略包唯一可 import 的框架契约）
    runner.py      OfflineRunner：解析 run → 策略 → 校验 → 幂等写回
    materials.py   各 stage 激活策略的物料核对
    impl/          一个策略一个自包含包，经 YAML `offline.class` 按全限定路径加载

与同级的 `compliance/`（规则判定）互不 import。纯包标记，消费方走深路径。
"""
