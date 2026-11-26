-- ============================================================================
-- RAG 知识库 · PostgreSQL 建表脚本（幂等，可重复执行）
-- ============================================================================
-- 在全新环境初始化数据库（二选一，口令走环境变量，不要写进命令行 / 文件 / shell 历史）：
--   PGPASSWORD='<口令>' psql -h 127.0.0.1 -U raguser -d rag_db -f sql/init.sql
--   docker exec -i postgres psql -U raguser -d rag_db < sql/init.sql
--
-- 脚本结构：
--   1. 用户 / 会话
--   2. 文档 / 分块
--   3. 解析任务
--   4. 问答日志
--   5. 老库升级（幂等；全新环境无需执行，跑了也无害）
--   6. 注释（放最后执行，保证老库补完列后再注释）
--
-- 特点：
--   * 所有建表/索引均为 IF NOT EXISTS，可反复执行；
--   * 列全部内联写在 CREATE TABLE 里，不再靠 ALTER 逐列打补丁；
--   * 老库升级段用 DO 块做存在性判断，脏数据只告警不中断（不会把脚本卡死）。
--
-- 数据模型要点（「上传记录」相关）：
--   * rag_doc 是文档的**权威表**：doc_name / file_md5 / file_size / page_count / status
--     上传时先插入一条 status='processing' 的占位行，解析完成后原地更新为 success / failed；
--   * rag_task 记录**每一次解析任务**，通过 doc_id 关联 rag_doc
--     （列表 = rag_doc LEFT JOIN 该 doc 最新一条 rag_task 取状态/进度/失败原因/上传人）；
--   * 同一 file_md5 只允许一条 rag_doc：失败记录同样占位，必须在「上传记录」里
--     由管理员删除后才能重新上传。
-- ============================================================================


-- ============================================================================
-- 1. 用户 / 会话
-- ============================================================================

-- 用户表（注册 / 登录 / 角色）
CREATE TABLE IF NOT EXISTS rag_user (
    user_id       VARCHAR(64) PRIMARY KEY,
    username      VARCHAR(64) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    nickname      TEXT,
    role          VARCHAR(16) DEFAULT 'user',    -- user / admin
    status        VARCHAR(16) DEFAULT 'active',  -- active / disabled
    -- token 撤销水位（epoch 秒）：token payload 里的 iat < 该值即失效。
    -- 故意用 BIGINT epoch 而不是 TIMESTAMP：本库里 created_at 是
    -- TIMESTAMP WITHOUT TIME ZONE + NOW()（服务器本地时区），
    -- 与 Python time.time() 的 UTC epoch 直接比会差一个时区偏移。
    token_epoch   BIGINT NOT NULL DEFAULT 0,
    created_at    TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at    TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    meta          JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_rag_user_username ON rag_user(username);

-- 会话主表（会话为个人私有）
CREATE TABLE IF NOT EXISTS chat_session (
    chat_id    VARCHAR(64) PRIMARY KEY,
    user_id    VARCHAR(64) DEFAULT 'default',
    title      TEXT,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    meta       JSONB DEFAULT '{}'::jsonb
);

-- 消息明细表
CREATE TABLE IF NOT EXISTS chat_message (
    msg_id      VARCHAR(64) PRIMARY KEY,
    chat_id     VARCHAR(64) NOT NULL REFERENCES chat_session(chat_id) ON DELETE CASCADE,
    role        VARCHAR(16) NOT NULL,            -- user / assistant
    content     TEXT NOT NULL,
    cost_tokens INTEGER DEFAULT 0,
    created_at  TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    meta        JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_msg_chat_id ON chat_message(chat_id);


-- ============================================================================
-- 2. 文档 / 分块
-- ============================================================================

-- 文档主表（一篇文档一行，是文件名 / md5 / 大小 / 页数 / 状态的权威来源）
CREATE TABLE IF NOT EXISTS rag_doc (
    doc_id     VARCHAR(64) PRIMARY KEY,
    doc_name   TEXT NOT NULL,
    file_path  TEXT,
    file_md5   VARCHAR(64),                       -- 幂等去重依据（同一文件只入库一次）
    file_size  BIGINT,                            -- 上传时的文件字节数
    page_count INT,
    status     VARCHAR(20) DEFAULT 'success',     -- processing（排队/解析中）/ success / failed
    meta       JSONB,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- 一个文件（md5）只允许一条 rag_doc 记录
CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_doc_file_md5
    ON rag_doc(file_md5) WHERE file_md5 IS NOT NULL;

-- 文档分块表（与 rag_doc 一对多，chunk_id 与 Qdrant point id 一致）
CREATE TABLE IF NOT EXISTS rag_chunk (
    chunk_id     VARCHAR(64) PRIMARY KEY,
    doc_id       VARCHAR(64) NOT NULL,
    doc_name     TEXT NOT NULL,
    doc_type     VARCHAR(32) NOT NULL,
    source_path  TEXT NOT NULL,
    import_time  TIMESTAMP NOT NULL,

    section_path TEXT NOT NULL,                   -- 标题继承路径：第一章 > 1.1 概述
    title        TEXT NOT NULL,
    title_num    TEXT,
    level        INTEGER NOT NULL,
    page_no      INTEGER,
    is_table     BOOLEAN DEFAULT false,
    token_len    INTEGER NOT NULL,
    content_md   TEXT NOT NULL,                   -- 原始 md 文本，用于溯源 / 重新向量化

    extra_meta   JSONB DEFAULT '{}'::JSONB,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_rag_chunk_doc FOREIGN KEY (doc_id) REFERENCES rag_doc(doc_id)
);

-- 按 doc_id 查询 / 删除非常高频
CREATE INDEX IF NOT EXISTS idx_rag_chunk_doc_id ON rag_chunk(doc_id);
CREATE INDEX IF NOT EXISTS idx_rag_chunk_page   ON rag_chunk(doc_id, page_no);


-- ============================================================================
-- 3. 解析任务（每次上传一条；通过 doc_id 与 rag_doc 关联）
-- ============================================================================

CREATE TABLE IF NOT EXISTS rag_task (
    task_id    VARCHAR(64) PRIMARY KEY,           -- 与 Celery task id 相同
    doc_id     VARCHAR(64),                       -- 上传时即确定（rag_doc 占位行）
    file_name  TEXT,
    file_path  TEXT,
    status     VARCHAR(20) NOT NULL,              -- pending / running / success / failed
    error_msg  TEXT,
    progress   INTEGER DEFAULT 0,                 -- 0 ~ 100
    user_id    VARCHAR(64),                       -- 上传人（rag_user.user_id）
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rag_task_doc_id   ON rag_task(doc_id);
CREATE INDEX IF NOT EXISTS idx_rag_task_status   ON rag_task(status);
CREATE INDEX IF NOT EXISTS idx_rag_task_user_id  ON rag_task(user_id);


-- ============================================================================
-- 4. 问答日志 & 反馈
-- ============================================================================

CREATE TABLE IF NOT EXISTS rag_qa_log (
    log_id               VARCHAR(64) PRIMARY KEY,
    query                TEXT NOT NULL,
    answer               TEXT NOT NULL,
    retrieved_chunk_ids  TEXT[],                  -- 召回的 chunk_id 列表
    rerank_scores        FLOAT[],
    doc_ids              TEXT[],
    user_feedback        INT DEFAULT NULL,        -- 1 满意 / -1 不满意 / NULL 未评价
    feedback_note        TEXT,
    cost_ms              INT,                     -- 总耗时
    meta                 JSONB,
    created_at           TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rag_qa_log_created_at ON rag_qa_log(created_at);


-- ============================================================================
-- 4.5 记忆（用户画像 / 偏好）
--     短期会话摘要不建表（存 chat_session.meta）；长期记忆只存“用户本人稳定信息”，
--     不存知识库事实（防止长期记忆污染检索边界）。
--     真相在 PG；Qdrant 集 rag_memory 只是语义索引（point id = memory_id）。
-- ============================================================================

CREATE TABLE IF NOT EXISTS rag_memory (
    memory_id      VARCHAR(64) PRIMARY KEY,
    user_id        VARCHAR(64) NOT NULL,
    kind           VARCHAR(16) NOT NULL,          -- profile（身份/领域/职责）/ preference（风格、范围、格式偏好）
    content        TEXT NOT NULL,                 -- 单条 ≤ memory.profile_max_chars 字
    content_hash   VARCHAR(64) NOT NULL,          -- 归一化后 md5 ⇒ 同一条画像说多遍只存一条
    weight         REAL DEFAULT 0.5,              -- 0~1；抽取时 LLM 给，命中时不抬高（只记 hits）
    hits           INTEGER DEFAULT 0,
    last_used_at   TIMESTAMP,
    expires_at     TIMESTAMP,                     -- 到期不再注入（防长期污染）
    status         VARCHAR(16) DEFAULT 'active',  -- active / archived（软删，Qdrant 里的点同步删）
    source_chat_id VARCHAR(64),
    meta           JSONB DEFAULT '{}'::jsonb,
    created_at     TIMESTAMP DEFAULT NOW(),
    updated_at     TIMESTAMP DEFAULT NOW()
);

-- 固定注入核心画像时走这个索引；完全按 user 隔离，没有任何管理员旁路
CREATE INDEX IF NOT EXISTS idx_rag_memory_user
    ON rag_memory(user_id, status, weight DESC, last_used_at DESC);
-- 去重依赖它（ON CONFLICT (user_id, content_hash)），跟 uq_rag_doc_file_md5 同思路
CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_memory_user_hash ON rag_memory(user_id, content_hash);


-- ============================================================================


-- ============================================================================
-- 5. 老库升级（幂等；全新环境无需执行，跑了也不会报错）
--    全部包在 DO 块里做存在性判断 / 异常兜底，避免老数据脏导致整份脚本中断
-- ============================================================================

DO $$
BEGIN
    ------------------------------------------------------------------------
    -- 6.1 很老的 rag_chunk：缺 doc_id 列 / 缺外键
    ------------------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'rag_chunk' AND column_name = 'doc_id') THEN
        ALTER TABLE rag_chunk ADD COLUMN doc_id VARCHAR(64);
        RAISE NOTICE '已为 rag_chunk 补 doc_id 列';
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_rag_chunk_doc') THEN
        BEGIN
            ALTER TABLE rag_chunk
                ADD CONSTRAINT fk_rag_chunk_doc FOREIGN KEY (doc_id) REFERENCES rag_doc(doc_id);
            RAISE NOTICE '已为 rag_chunk.doc_id 添加外键';
        EXCEPTION WHEN others THEN
            RAISE NOTICE '跳过 rag_chunk.doc_id 外键（存在孤儿 chunk 或已有同名约束）：%', SQLERRM;
        END;
    END IF;

    ------------------------------------------------------------------------
    -- 6.2 rag_doc.file_size（原先只存在 rag_task 上）
    ------------------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'rag_doc' AND column_name = 'file_size') THEN
        ALTER TABLE rag_doc ADD COLUMN file_size BIGINT;
        RAISE NOTICE '已为 rag_doc 补 file_size 列';
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'rag_task' AND column_name = 'file_size') THEN
        UPDATE rag_doc d
           SET file_size = t.file_size
          FROM (SELECT DISTINCT ON (doc_id) doc_id, file_size
                  FROM rag_task
                 WHERE file_size IS NOT NULL
                 ORDER BY doc_id, created_at DESC) t
         WHERE t.doc_id = d.doc_id AND d.file_size IS NULL;
        RAISE NOTICE '已把 rag_task.file_size 回填到 rag_doc';
    END IF;

    ------------------------------------------------------------------------
    -- 6.3 rag_task.user_id（上传人）
    ------------------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'rag_task' AND column_name = 'user_id') THEN
        ALTER TABLE rag_task ADD COLUMN user_id VARCHAR(64);
        RAISE NOTICE '已为 rag_task 补 user_id 列';
    END IF;

    ------------------------------------------------------------------------
    -- 6.4 删掉 rag_task 上的冗余列（md5 / 大小 已归 rag_doc）
    ------------------------------------------------------------------------
    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'rag_task' AND column_name = 'file_md5') THEN
        DROP INDEX IF EXISTS idx_rag_task_file_md5;
        ALTER TABLE rag_task DROP COLUMN file_md5;
        RAISE NOTICE '已删除冗余列 rag_task.file_md5';
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
                WHERE table_name = 'rag_task' AND column_name = 'file_size') THEN
        ALTER TABLE rag_task DROP COLUMN file_size;
        RAISE NOTICE '已删除冗余列 rag_task.file_size';
    END IF;

    ------------------------------------------------------------------------
    -- 6.5 rag_user.token_epoch（token 可撤销，P0 #7）
    --     新库已在 CREATE TABLE 里内联；老库在这里补列。
    --     默认 0 ⇒ 升级当天已有的 token 仍然有效（不踢人），
    --     直到管理员改密 / 改角色 / 禁用才会被 bump。
    ------------------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'rag_user' AND column_name = 'token_epoch') THEN
        BEGIN
            ALTER TABLE rag_user ADD COLUMN token_epoch BIGINT NOT NULL DEFAULT 0;
            RAISE NOTICE '已为 rag_user 补 token_epoch 列';
        EXCEPTION WHEN others THEN
            RAISE NOTICE '跳过 rag_user.token_epoch（已存在或其它原因）：%', SQLERRM;
        END;
    END IF;

    ------------------------------------------------------------------------
    -- 6.6 md5 唯一索引（老库可能有同 md5 重复记录，失败只告警不中断）
    ------------------------------------------------------------------------
    IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'uq_rag_doc_file_md5') THEN
        BEGIN
            CREATE UNIQUE INDEX uq_rag_doc_file_md5
                ON rag_doc(file_md5) WHERE file_md5 IS NOT NULL;
            RAISE NOTICE '已创建 uq_rag_doc_file_md5';
        EXCEPTION WHEN others THEN
            RAISE NOTICE '未能创建 uq_rag_doc_file_md5（历史数据存在重复 md5，请先清理）：%', SQLERRM;
            RAISE NOTICE '排查语句: SELECT file_md5, COUNT(*) FROM rag_doc WHERE file_md5 IS NOT NULL GROUP BY file_md5 HAVING COUNT(*) > 1;';
        END;
    END IF;
END $$;


-- ============================================================================
-- 6. 注释（放在最后：老库升级段补完列之后再执行）
-- ============================================================================

COMMENT ON COLUMN rag_user.token_epoch     IS 'token 撤销水位（epoch 秒）：token 的 iat 小于该值即失效；改密/改角色/禁用时更新';

COMMENT ON TABLE  rag_memory                   IS '用户长期记忆（仅画像/偏好；PG 为真相，Qdrant rag_memory 集只做语义索引）';
COMMENT ON COLUMN rag_memory.kind              IS 'profile=身份/领域/职责, preference=风格/范围/格式偏好';
COMMENT ON COLUMN rag_memory.content_hash      IS '归一化(去空白+大小写)后 md5，同用户去重靠它（ON CONFLICT）';
COMMENT ON COLUMN rag_memory.expires_at        IS '到期不再注入 prompt；默认 created_at + memory.default_ttl_days';
COMMENT ON COLUMN rag_memory.status            IS 'active / archived（软删，同时删 Qdrant 向量点）';

COMMENT ON TABLE  rag_chunk                IS 'RAG文档分块结构化存储';
COMMENT ON COLUMN rag_chunk.chunk_id       IS 'chunk唯一uuid，和qdrant point id一致';
COMMENT ON COLUMN rag_chunk.doc_id         IS '整篇文档唯一id';
COMMENT ON COLUMN rag_chunk.section_path   IS '标题继承路径，例如 第一章 > 1.1 概述';
COMMENT ON COLUMN rag_chunk.content_md     IS '原始md文本，用于溯源、重新向量化';

COMMENT ON TABLE  rag_doc                  IS '文档主表（文件名/md5/大小/页数/状态 的权威来源）';
COMMENT ON COLUMN rag_doc.file_md5         IS '文件md5，同md5只允许一条记录（失败记录也占位）';
COMMENT ON COLUMN rag_doc.file_size        IS '上传时的文件字节数';
COMMENT ON COLUMN rag_doc.status           IS 'processing=排队/解析中, success, failed';

COMMENT ON TABLE  rag_task                 IS '文档解析任务（每次上传一条，doc_id 关联 rag_doc）';
COMMENT ON COLUMN rag_task.task_id         IS '与 Celery task id 一致';
COMMENT ON COLUMN rag_task.doc_id          IS '上传时即确定的 doc_id（同一文档多次任务取最新一条）';
COMMENT ON COLUMN rag_task.user_id         IS '上传人 rag_user.user_id';
COMMENT ON COLUMN rag_task.status          IS 'pending / running / success / failed';
