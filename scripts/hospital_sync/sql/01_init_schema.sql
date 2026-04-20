-- 01_init_schema.sql
-- 创建测试库、表、视图

USE [HospitalDB];
GO

-- ─── 患者基本信息 ────────────────────────────────────────────────────────────
CREATE TABLE patient (
    patient_id      VARCHAR(20)  PRIMARY KEY,   -- 病历号
    name            NVARCHAR(20) NOT NULL,       -- 姓名
    gender          NCHAR(1)     NOT NULL,       -- 性别：男/女
    id_card         VARCHAR(18)  NOT NULL,       -- 身份证号
    insurance_card  VARCHAR(30)  NULL            -- 医保卡号
);
GO

-- ─── 检查日志 ─────────────────────────────────────────────────────────────────
CREATE TABLE exam_log (
    order_no    VARCHAR(20)    PRIMARY KEY,      -- 申请单序号
    patient_id  VARCHAR(20)    NOT NULL,         -- 病历号
    item_name   NVARCHAR(50)   NOT NULL,         -- 项目名称
    item_code   VARCHAR(20)    NOT NULL,         -- 项目代码
    cost        DECIMAL(10,2)  NOT NULL,         -- 费用（元）
    exam_time   DATETIME       NOT NULL DEFAULT GETDATE()
);
GO

-- ─── 挂号信息 ─────────────────────────────────────────────────────────────────
CREATE TABLE registration (
    reg_no      VARCHAR(20)   PRIMARY KEY,       -- 挂号单号
    patient_id  VARCHAR(20)   NOT NULL,          -- 病历号
    dept        NVARCHAR(20)  NOT NULL,          -- 科室
    doctor      NVARCHAR(20)  NOT NULL,          -- 接诊医生
    reg_type    NVARCHAR(10)  NOT NULL,          -- 挂号类型：普通/专家
    fee         DECIMAL(8,2)  NOT NULL,          -- 挂号费
    reg_time    DATETIME      NOT NULL DEFAULT GETDATE()
);
GO

-- ─── 视图（sync_agent 读取全库视图） ─────────────────────────────────────────
CREATE VIEW v_patient AS
    SELECT patient_id, name, gender, id_card, insurance_card
    FROM patient;
GO

CREATE VIEW v_exam_log AS
    SELECT e.order_no, e.patient_id, p.name AS patient_name,
           e.item_name, e.item_code, e.cost, e.exam_time
    FROM exam_log e
    JOIN patient p ON e.patient_id = p.patient_id;
GO

CREATE VIEW v_registration AS
    SELECT r.reg_no, r.patient_id, p.name AS patient_name,
           r.dept, r.doctor, r.reg_type, r.fee, r.reg_time
    FROM registration r
    JOIN patient p ON r.patient_id = p.patient_id;
GO
