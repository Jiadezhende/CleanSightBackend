// step 能力表（GET /admin-f3m8/stages）：启动时拉一次，前端不写死 step 别名与能力。
import { api } from './endpoints.js';

const { reactive } = Vue;

export const catalog = reactive({ stages: [], loaded: false });

export async function loadCatalog() {
  const d = await api.stages();
  catalog.stages = d.stages;
  catalog.loaded = true;
}

/** 配置里没有的 step 按「无别名、无离线模型」处理，显示主键。 */
export function stageOf(stepId) {
  return catalog.stages.find((s) => s.step_id === Number(stepId))
    || { step_id: Number(stepId), alias: String(stepId), has_offline_model: false, has_compliance: false };
}
