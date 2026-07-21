# test prod环境端口隔离

> **变更状态**：生效中（历史记录，本次维护确认）
> **知识库**：已沉淀 → [kb/SERVICE_CONFIG.md](../kb/SERVICE_CONFIG.md)(2026-07-21)
start_backend一次性启动mediamtx和后端app两个服务并且分配好对应环境的接口：test为8100，8104；prod和dev为8000，8004