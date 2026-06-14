-- CleanSightBackend minimal PostgreSQL schema.
-- Safe to run more than once. It creates only missing tables/indexes.

CREATE TABLE IF NOT EXISTS clean_task (
    _id TEXT PRIMARY KEY DEFAULT md5(random()::text || clock_timestamp()::text),
    cls_id TEXT NOT NULL DEFAULT 'clean_task',
    task_id BIGINT NOT NULL,
    source_ip TEXT,
    current_step TEXT DEFAULT '0',
    status TEXT DEFAULT 'paused',
    updated_time BIGINT,
    start_time BIGINT DEFAULT 0,
    end_time BIGINT DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_clean_task_task_id_unique
    ON clean_task (task_id);

CREATE INDEX IF NOT EXISTS idx_clean_task_source_ip
    ON clean_task (source_ip);

CREATE INDEX IF NOT EXISTS idx_clean_task_updated_time
    ON clean_task (updated_time DESC);


CREATE TABLE IF NOT EXISTS clean_alarm (
    _id TEXT PRIMARY KEY DEFAULT md5(random()::text || clock_timestamp()::text),
    alarm_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    step_id BIGINT,
    step_name TEXT,
    alarm_type TEXT,
    severity TEXT,
    message TEXT,
    detected_at BIGINT,
    resolved BOOLEAN DEFAULT FALSE,
    resolved_by BIGINT,
    resolved_at BIGINT,
    create_time BIGINT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_clean_alarm_alarm_id_unique
    ON clean_alarm (alarm_id);

CREATE INDEX IF NOT EXISTS idx_clean_alarm_task_id
    ON clean_alarm (task_id);

CREATE INDEX IF NOT EXISTS idx_clean_alarm_task_step
    ON clean_alarm (task_id, step_id);

CREATE INDEX IF NOT EXISTS idx_clean_alarm_create_time
    ON clean_alarm (create_time DESC);

CREATE INDEX IF NOT EXISTS idx_clean_alarm_detected_at
    ON clean_alarm (detected_at);
