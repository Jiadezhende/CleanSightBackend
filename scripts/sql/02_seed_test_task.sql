-- Optional test data for the lab task list page.

INSERT INTO clean_task (
    task_id,
    source_ip,
    current_step,
    status,
    updated_time
) VALUES (
    1001,
    '127.0.0.1',
    '1',
    'completed',
    (extract(epoch from now()) * 1000)::bigint
)
ON CONFLICT (task_id) DO UPDATE SET
    source_ip = EXCLUDED.source_ip,
    current_step = EXCLUDED.current_step,
    status = EXCLUDED.status,
    updated_time = EXCLUDED.updated_time;
