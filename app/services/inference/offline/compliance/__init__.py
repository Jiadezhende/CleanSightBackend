"""离线规则判定（合规核对）—— 读 run 的离线产物，按该 step 的规则表判定，违规疑似转 `Alarm` 落 `alarms.jsonl`。

离线包里与动作分割并列的另一个功能模块：分割（`segmenter` / `runner` / `materials` / `impl/`）产出逐帧概率，
本包只读盘上产物做判定，两边互不 import，只由 `offline/cli.py` 串起来（门禁见 `tests/test_import_hygiene.py`）。

    config.py    规则表 dict → ComplianceConfig
    evidence.py  逐帧概率 + 检测结果 → Evidence
    rules.py     五种通用规则 → Verdict
    checker.py   编排：读盘 → 判定 → Alarm → 写盘

纯包标记，消费方走深路径。
"""
