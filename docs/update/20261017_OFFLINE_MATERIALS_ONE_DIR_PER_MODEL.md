# 离线物料统一为一个模型一个目录，YAML / README / API 示例指向新策略包

> **变更状态**：生效中（2026-10-17）；**已部署机器须按下文一次性挪动物料**
> **知识库**：待沉淀

## 概述

历史离线策略的扁平权重文件改放进各自的模型目录（`app/data/clean-offline-<模型>/model.pt`），与 mstcn2-role-embed 同一条规则。
YAML 注释改为指向各策略包 docstring 里的 YAML 片段；每个包的 YAML 片段改成合法的块格式，并新增用例保证它能直接装配出对应策略。
README、`docs/api/admin.md`、`docs/api/ai.md` 的 producer 示例改为当前默认 `CleanMSTCN2RoleEmbedSegmenter`。

## 变更背景

- **承接**：[20261017_OFFLINE_LEGACY_V2_PACKAGES.md](20261017_OFFLINE_LEGACY_V2_PACKAGES.md)、[20261017_OFFLINE_LEGACY_REST_PACKAGES.md](20261017_OFFLINE_LEGACY_REST_PACKAGES.md)
  里的新包读 `model_dir/model.pt`，旧扁平文件只有旧 `impl/clean.py` 在用。
- **发现的问题**：各包 docstring 原写的是 `params: {model_dir: ${CLEANSIGHT_MODEL_PATH:./app/data}/…}` 流式写法，`${…}` 里的花括号会让
  YAML 解析失败，粘贴进 stage 就报错。已全部改为块格式。

## 方案详情

### 物料挪动（部署机一次性执行，不入库脚本）

```bash
cd "${CLEANSIGHT_MODEL_PATH:-./app/data}"
mkdir -p clean-offline-mstcn-bilstm clean-offline-asformer clean-offline-bigru clean-offline-gru-nodep clean-offline-mstcn2-bundle
mv clean-offline-mstcn.pt           clean-offline-mstcn-bilstm/model.pt
mv clean-offline-asformer.pt        clean-offline-asformer/model.pt
mv clean-offline-bigru.pt           clean-offline-bigru/model.pt
mv clean-offline-gru-nodep.pt       clean-offline-gru-nodep/model.pt
mv clean-offline-mstcn2-v5bundle.pt clean-offline-mstcn2-bundle/model.pt   # 只有装过 v5-bundle 的机器有
cd - && python -m app.services.inference.offline.cli materials             # 核对当前激活策略的物料
```

回滚：反向 `mv` 即可；旧 `impl/clean.py` 读扁平文件，删除它之前两套路径可以并存。

### 物料目录全貌

```text
app/data/
  clean-offline-mstcn-bilstm/model.pt          clean_mstcn_bilstm      带元数据训练导出（weights_only=False）
  clean-offline-asformer/model.pt              clean_asformer          同上
  clean-offline-bigru/model.pt                 clean_bigru             同上
  clean-offline-gru-nodep/model.pt             clean_gru_nodep         训练状态（只取 model_state）
  clean-offline-mstcn2-bundle/model.pt         clean_mstcn2_bundle     训练状态（只取 model_state）
  clean-offline-mstcn2-role-embed/{temporal.pt, hand_state.npz}   当前默认   纯 state_dict + npz
  backbones/dinov2_vits14/                     DINOv2 hub 代码 + 权重
```

## 变更效果

| 项 | 结果 |
|----|------|
| 本机按各包 docstring 的 YAML 片段装配 + 真实权重前向（300 帧合成序列） | mstcn-bilstm / asformer / bigru / mstcn2-bundle 全部跑通；gru-nodep 本机无权重，物料清单如实报缺 |
| 新增 `tests/test_offline_impl_catalog.py` | 每个策略包的 YAML 片段：合法 YAML、class 指向本包、StageFactory 能装配、`model_dir` 名 = `clean-offline-<模型>`、物料都在声明的目录下 |
| 全量 `pytest tests/` | 1123 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 部署机没挪物料就切到历史策略 | 该 stage 离线作业 `FileNotFoundError`（启动预检会先 WARNING） | 按上文命令挪动；`cli materials` 核对 |
| 旧 `impl/clean.py` 与 `tests/test_offline_clean_legacy.py` 仍在 | 无调用点 | 下一批单独删除 |
