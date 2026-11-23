CREATE TABLE IF NOT EXISTS rag_chunk (
    chunk_id VARCHAR(64) PRIMARY KEY,
    doc_id VARCHAR(64) NOT NULL,
    doc_name TEXT NOT NULL,
    doc_type VARCHAR(32) NOT NULL,
    source_path TEXT NOT NULL,
    import_time TIMESTAMP NOT NULL,

    section_path TEXT NOT NULL,
    title TEXT NOT NULL,
    title_num TEXT,
    level INTEGER NOT NULL,
    page_no INTEGER,
    is_table BOOLEAN DEFAULT false,
    token_len INTEGER NOT NULL,
    content_md TEXT NOT NULL,

    extra_meta JSONB DEFAULT '{}'::JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 索引：按doc_id查询、删除非常高频
CREATE INDEX idx_rag_chunk_doc_id ON rag_chunk(doc_id);
CREATE INDEX idx_rag_chunk_page ON rag_chunk(doc_id, page_no);

COMMENT ON TABLE rag_chunk IS 'RAG文档分块结构化存储';
COMMENT ON COLUMN rag_chunk.chunk_id IS 'chunk唯一uuid，和qdrant point id一致';
COMMENT ON COLUMN rag_chunk.doc_id IS '整篇文档唯一id';
COMMENT ON COLUMN rag_chunk.section_path IS '标题继承路径，例如 第一章 > 1.1 概述';
COMMENT ON COLUMN rag_chunk.content_md IS '原始md文本，用于溯源、重新向量化';

-- 文档主表（doc 表，管理文档维度，和 rag_chunk 是一对多）
CREATE TABLE IF NOT EXISTS rag_doc (
    doc_id VARCHAR(64) PRIMARY KEY,
    doc_name TEXT NOT NULL,
    file_path TEXT,
    file_md5 VARCHAR(64), -- 用来做幂等：同一个文件md5不变就不重复入库
    page_count INT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'success', -- success / processing / failed
    meta JSONB
);

-- 你原来的chunk表外键关联（可选，方便联查）
ALTER TABLE rag_chunk ADD COLUMN IF NOT EXISTS doc_id VARCHAR(64) REFERENCES rag_doc(doc_id);
CREATE INDEX idx_rag_chunk_doc_id ON rag_chunk(doc_id);

-- 问答日志 & 反馈表（feedback）
CREATE TABLE IF NOT EXISTS rag_qa_log (
    log_id VARCHAR(64) PRIMARY KEY,
    query TEXT NOT NULL,
    answer TEXT NOT NULL,
    retrieved_chunk_ids TEXT[], -- 召回的chunk_id列表
    rerank_scores FLOAT[],
    doc_ids TEXT[],
    user_feedback INT DEFAULT NULL, -- 1满意 / -1不满意 / NULL未评价
    feedback_note TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    cost_ms INT, -- 总耗时
    meta JSONB
);
CREATE INDEX idx_rag_qa_log_created_at ON rag_qa_log(created_at);

-- 新建 PG 任务状态表（放到你的 SQL 脚本）
CREATE TABLE IF NOT EXISTS rag_task (
    task_id VARCHAR(64) PRIMARY KEY,
    doc_id VARCHAR(64),
    file_name TEXT,
    file_path TEXT,
    status VARCHAR(20) NOT NULL, -- pending / running / success / failed
    error_msg TEXT,
    progress INTEGER DEFAULT 0, -- 0~100
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_rag_task_doc_id ON rag_task(doc_id);
CREATE INDEX idx_rag_task_status ON rag_task(status);

-- PG 建表 SQL（chat_session + chat_message）
-- 会话主表
CREATE TABLE IF NOT EXISTS chat_session (
    chat_id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) DEFAULT 'default',
    title TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    meta JSONB DEFAULT '{}'::jsonb
);

-- 消息明细表
CREATE TABLE IF NOT EXISTS chat_message (
    msg_id VARCHAR(64) PRIMARY KEY,
    chat_id VARCHAR(64) NOT NULL REFERENCES chat_session(chat_id) ON DELETE CASCADE,
    role VARCHAR(16) NOT NULL, -- user / assistant
    content TEXT NOT NULL,
    cost_tokens INTEGER DEFAULT 0,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    meta JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX idx_msg_chat_id ON chat_message(chat_id);

-- 用户表（注册/登录）
CREATE TABLE IF NOT EXISTS rag_user (
    user_id VARCHAR(64) PRIMARY KEY,
    username VARCHAR(64) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    nickname TEXT,
    role VARCHAR(16) DEFAULT 'user', -- user / admin
    status VARCHAR(16) DEFAULT 'active', -- active / disabled
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    meta JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX idx_rag_user_username ON rag_user(username);
