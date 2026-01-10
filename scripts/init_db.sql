-- 初始化脚本（针对 PostgreSQL）
-- 用法：
-- psql -h <host> -p <port> -U <user> -d <db> -f scripts/init_db.sql
--
-- 本脚本基于以下 SQLAlchemy 模型结构生成：
--   - app.models.task.DBTask -> clean_task
--   - app.models.frame.HLSSegment -> file_path

CREATE TABLE IF NOT EXISTS clean_task (
    task_id SERIAL PRIMARY KEY,
    source_ip TEXT,
    current_step TEXT DEFAULT '0',
    status TEXT DEFAULT 'paused',
    updated_time BIGINT,
    start_time BIGINT DEFAULT 0,
    end_time BIGINT DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_clean_task_source_ip ON clean_task(source_ip);
CREATE INDEX IF NOT EXISTS idx_clean_task_updated_time ON clean_task(updated_time);

CREATE TABLE IF NOT EXISTS file_path (
    _id SERIAL PRIMARY KEY,
    client_id TEXT,
    task_id INTEGER,
    segment_path TEXT,
    playlist_path TEXT,
    start_ts BIGINT,
    end_ts BIGINT,
    created_at BIGINT DEFAULT (EXTRACT(EPOCH FROM now())::bigint)
);

CREATE INDEX IF NOT EXISTS idx_file_path_client_id ON file_path(client_id);
CREATE INDEX IF NOT EXISTS idx_file_path_task_id ON file_path(task_id);
