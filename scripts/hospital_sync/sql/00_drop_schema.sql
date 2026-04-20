-- 00_drop_schema.sql
-- 清理所有视图与表（执行顺序：视图 → 子表 → 主表）

-- ─── 视图 ─────────────────────────────────────────────────────────────────────
IF OBJECT_ID('v_registration', 'V') IS NOT NULL DROP VIEW v_registration;
IF OBJECT_ID('v_exam_log',     'V') IS NOT NULL DROP VIEW v_exam_log;
IF OBJECT_ID('v_patient',      'V') IS NOT NULL DROP VIEW v_patient;
GO

-- ─── 表（子表先删，避免外键约束报错） ─────────────────────────────────────────
IF OBJECT_ID('registration', 'U') IS NOT NULL DROP TABLE registration;
IF OBJECT_ID('exam_log',     'U') IS NOT NULL DROP TABLE exam_log;
IF OBJECT_ID('patient',      'U') IS NOT NULL DROP TABLE patient;
GO
