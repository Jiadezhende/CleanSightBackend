"""离线动作分割 —— detections.jsonl → `OfflineSegmenter` 策略 → temporal.jsonl + label_probs.npz。

    segmenter.py   策略基类、运行上下文与推理结果
    utils.py       帧准备与推理输出校验
    materials.py   各 stage 激活策略的物料核对
    impl/          一个策略一个自包含包，经 YAML `offline.class` 按全限定路径加载

与同级的 `compliance/`（规则判定）互不 import。纯包标记，消费方走深路径。
"""
