from pathlib import Path
from typing import List
import os
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml

BASE_DIR = Path(__file__).parent                 # src/
PROJECT_ROOT = BASE_DIR.parent                   # 仓库根（config/ 的上一级）


def resolve_path(p) -> Path:
    """把配置里的路径解析成绝对路径。

    绝对路径原样返回；相对路径按仓库根（config/ 的上一级）解析。
    为什么不用 CWD：API 进程与 Celery worker 的 CWD 可以不同，
    按配置文件位置解析 ⇒ 两个进程在任何目录下都算出同一个绝对路径。
    """
    path = Path(p)
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


# 配置文件位置：默认仓库根的 config/config.yaml（该文件已被 .gitignore 排除，
# 模板是 config/config.yaml.example）。
# 生产环境可以用 RAG_CONFIG 指到仓库外（真实凭据就不在代码目录里了）：
#   PowerShell: $env:RAG_CONFIG = "D:/rag_secrets/myrag.yaml"
#   bash:       export RAG_CONFIG=/etc/myrag/config.yaml
# 相对路径按仓库根解析；API 进程与 celery worker 必须指同一个文件。
_CONFIG_ENV = os.environ.get("RAG_CONFIG", "").strip()
config_path = resolve_path(_CONFIG_ENV) if _CONFIG_ENV else (PROJECT_ROOT / "config" / "config.yaml")


class DoclingParserSettings(BaseSettings):
    docling_artifacts_dir: Path
    output_dir: Path
    # 是否记录页码（P0 #4）。默认关：现有业务基本用不到页码，而且关掉后
    # 连页边界占位符都不会用，行为与修复前逐字一致。
    # 开启后：docling 导出时带页边界占位符，每个 chunk 写入 page_no
    #（跨页时额外在 extra_meta.page_span 里写页范围）。
    enable_page_no: bool = False

class MarkdownPreprocessorSettings(BaseSettings):
    chunk_size: int
    overlap: int
    min_chunk_token: int
    embedding_model_name: Path
    local_files_only: bool
    enable_log: bool
    skip_title_only_section: bool

class EmbeddingSettings(BaseSettings):
    model_name: Path
    local_files_only: bool
    device: str = "cuda"
    embed_url: str
    embed_health_url: str

class RerankSettings(BaseSettings):
    model_name: Path
    local_files_only: bool
    rerank_url: str
    rerank_health_url: str

class LlmSettings(BaseSettings):
    base_url: str
    api_key: str
    model_name: str

class ChatSettings(BaseSettings):
    max_total_tokens: int


class AuthSettings(BaseSettings):
    """鉴权相关配置（P0：签名密钥从环境变量改成优先读 yaml）。

    secret_key = token 签名密钥。两种存放方式，环境变量优先：
      1) 环境变量 RAG_SECRET_KEY（方便以后用 docker -e / systemd EnvironmentFile 注入）
      2) 本文件的 auth.secret_key（config/config.yaml 已被 .gitignore 排除 ⇒ 只存本地，
         和 pg/qdrant 凭据一起备份、一起迁移；多个进程读同一份 ⇒ 不会出现密钥不一致导致的随机掉线）
    两个都没填 ⇒ 服务拒绝启动（见 user_store._load_secret_key）。

    env_prefix 显式设为 RAG_AUTH_：避免字段名 secret_key 被其它进程的 SECRET_KEY 环境变量顶掉。
    要想用环境变量覆盖就写 RAG_AUTH_SECRET_KEY（但更建议用 RAG_SECRET_KEY）。
    """
    model_config = SettingsConfigDict(env_prefix="RAG_AUTH_")

    # 留空 ⇒ 回退到环境变量 RAG_SECRET_KEY；两边都空则拒绝启动
    secret_key: str = ""
    # token 有效期（秒），默认 7 天；环境变量 RAG_TOKEN_TTL 优先
    token_ttl: int = 604800


class RetrievalSettings(BaseSettings):
    """召回 / 重排参数（P0 #6：以前没有任何分数阈值，“未找到”几乎不会触发）。

    默认值依据：
      - rerank_min_score=0.10：官方模型卡 BAAI/bge-reranker-v2-m3 的实例分数里，
        相关对 sigmoid ≈ 0.9948，不相关对 sigmoid ≈ 0.000278 / 0.0035，
        0.10 在“不相关带”之上、临界带之下，不会误杀真实召回。
      - cosine_min_score=0.35：bge-m3 的余弦分布很压缩（不相关的中文长块常见
        0.30~0.40，相关长块 0.45~0.65），只剪“明显无关”。
        （Weaviate 那个 default_similarity=0.70 是按 OpenAI 系模型标定的，
        用在 bge-m3 上会把召回卡死，不采用。）
    两个阈值都是“下限”：调成 0 即关掉过滤；调高则更多提问回答“未找到”。
    事后可以从 rag_qa_log.meta.cosine_scores 看真实分布再定标。
    """
    recall_top_k: int = 6
    rerank_top_n: int = 3
    cosine_min_score: float = 0.35
    rerank_min_score: float = 0.10

class MemorySettings(BaseSettings):
    """长短记忆（会话摘要 + 用户画像）总配置。默认全关。

    - enable=false ⇒ prompt 里不会出现任何记忆段落，行为与加记忆之前逐字一致；
    - 灰度顺序：enable=true → short_term.enable_summary → long_term.enable_profile
      → long_term.enable_vector；出问题只回退一层。
    记忆任何环节失败都只打日志，不影响问答主链路。

    注：env_prefix 显式设成 RAG_MEMORY_，避免 enable 这种短字段名被环境变量误顶。
    """
    model_config = SettingsConfigDict(env_prefix="RAG_MEMORY_")

    enable: bool = False                                  # 总开关
    qdrant_collection_name: str = "rag_memory"            # 记忆向量集（与文档集分开）

    enable_summary: bool = True        # 短期：会话滚动摘要（enable=true 时生效）
    keep_recent_turns: int = 6         # 最近 N 轮永远当原文，不进摘要
    summary_trigger_ratio: float = 0.6 # 历史估算 token 超过 ratio×max_total_tokens 才压
    summary_max_tokens: int = 400      # 摘要本身的 token 上限

    enable_profile: bool = True        # 长期：画像/偏好条目（PG）+ 异步抽取
    enable_vector: bool = True         # 长期：语义召回（Qdrant）
    profile_limit: int = 12            # 固定注入的核心画像条数
    profile_max_chars: int = 60        # 单条字数上限
    recall_top_k: int = 4              # 语义召回条数
    recall_min_score: float = 0.45     # 记忆条目是短文本，分数分布与文档块不同，单独调
    max_items_per_user: int = 200      # 超出后按 (weight, last_used_at) 归档
    default_ttl_days: int = 180        # expires_at 默认值，防长期污染
    max_new_items: int = 5             # 单次抽取最多写入几条


class QdrantSettings(BaseSettings):
    host: str
    port: int
    https: bool = False
    api_key: str = None
    collection_name: str
    collection_shard_number: int
    collection_replication_factor: int
    collection_write_consistency_factor: int

class PgSettings(BaseSettings):
    host: str
    port: int
    dbname: str
    user: str
    password: str

class CelerySettings(BaseSettings):
    broker_url: str
    backend_url: str


class UploadSettings(BaseSettings):
    """上传相关配置（D1：全部放 config.yaml，不用环境变量）。

    dir 支持两种写法：
      - 相对路径：按仓库根解析（默认 "src/upload_cache"）
      - 绝对路径：原样使用（生产环境挂数据卷时更直观）
    所有默认值必须写在代码里，这样旧的 config.yaml 缺 upload 段也能正常加载。
    """
    dir: Path = Path("src/upload_cache")
    max_size_mb: int = 50
    allowed_suffixes: List[str] = [
        ".pdf", ".docx", ".pptx", ".xlsx", ".md", ".html", ".htm", ".txt",
    ]

    def suffix_set(self) -> set:
        """后缀白名单集合（容忍 yaml 里写不带点的 pdf）"""
        out = set()
        for s in self.allowed_suffixes:
            s = str(s).strip().lower()
            if s:
                out.add(s if s.startswith(".") else f".{s}")
        return out


class AppSettings(BaseSettings):
    debug: bool = False
    log_level: str = "INFO"
    docling_parser: DoclingParserSettings
    markdown_preprocessor: MarkdownPreprocessorSettings
    embedding: EmbeddingSettings
    rerank: RerankSettings
    llm: LlmSettings
    chat: ChatSettings
    auth: AuthSettings = AuthSettings()
    qdrant: QdrantSettings
    pg: PgSettings
    celery: CelerySettings
    # 必须有默认值，否则旧 config.yaml 没有 retrieval 段会直接加载失败
    retrieval: RetrievalSettings = RetrievalSettings()
    # 必须有默认值，否则旧 config.yaml 没有 memory 段会直接加载失败
    memory: MemorySettings = MemorySettings()
    # 必须有默认值，否则旧 config.yaml 没有 upload 段会直接加载失败
    upload: UploadSettings = UploadSettings()

    @staticmethod
    def _find_placeholders(node, prefix: str = "") -> List[str]:
        """找出还没填的模板占位值（CHANGE_ME），给出字段路径。"""
        hits: List[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                hits.extend(AppSettings._find_placeholders(value, path))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                hits.extend(AppSettings._find_placeholders(value, f"{prefix}[{i}]"))
        elif isinstance(node, str) and "CHANGE_ME" in node:
            hits.append(prefix or "(顶层)")
        return hits

    @classmethod
    def from_yaml(cls, path: Path):
        print(f"尝试加载配置文件：{path.resolve()}")
        if not path.exists():
            raise FileNotFoundError(
                f"配置文件不存在 -> {path.resolve()}\n"
                "  仓库里只放模板（config/config.yaml.example），真实配置不入库。先复制一份再填值：\n"
                "    PowerShell: Copy-Item config/config.yaml.example config/config.yaml\n"
                "    bash:       cp config/config.yaml.example config/config.yaml\n"
                "  也可以把真实配置放仓库外，用环境变量指过来：\n"
                "    PowerShell: $env:RAG_CONFIG = \"D:/rag_secrets/myrag.yaml\"\n"
                "    bash:       export RAG_CONFIG=/etc/myrag/config.yaml\n"
                "  注意：API 进程与 celery worker 必须读同一个配置文件。"
            )
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RuntimeError(f"YAML语法错误：{e}")

        if data is None:
            print("警告：yaml解析结果为None，文件为空或者只有注释！将使用默认配置")
            data = {}

        # 从模板复制过来但忘了填值：直接报明白，比后面“连不上服务”好排查
        placeholders = cls._find_placeholders(data)
        if placeholders:
            raise RuntimeError(
                "配置里还有没填的占位值（CHANGE_ME），请先把它们换掉：\n  - "
                + "\n  - ".join(placeholders)
            )
        # print(f"解析到的配置原始数据: {data}")
        return cls(**data)

settings = AppSettings.from_yaml(config_path)