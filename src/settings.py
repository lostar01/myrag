from pathlib import Path
from pydantic_settings import BaseSettings
import yaml

BASE_DIR = Path(__file__).parent
config_path = BASE_DIR.parent / "config" / "config.yaml"

class DoclingParserSettings(BaseSettings):
    docling_artifacts_dir: Path
    output_dir: Path

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

class RerankSettings(BaseSettings):
    model_name: Path
    local_files_only: bool

class LlmSettings(BaseSettings):
    base_url: str
    api_key: str
    model_name: str

class ChatSettings(BaseSettings):
    max_total_tokens: int

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
    
class AppSettings(BaseSettings):
    debug: bool = False
    log_level: str = "INFO"
    docling_parser: DoclingParserSettings
    markdown_preprocessor: MarkdownPreprocessorSettings
    embedding: EmbeddingSettings
    rerank: RerankSettings
    llm: LlmSettings
    chat: ChatSettings
    qdrant: QdrantSettings
    pg: PgSettings
    celery: CelerySettings

    @classmethod
    def from_yaml(cls, path: Path):
        print(f"尝试加载配置文件：{path.resolve()}")
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在 -> {path.resolve()}")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise RuntimeError(f"YAML语法错误：{e}")

        if data is None:
            print("警告：yaml解析结果为None，文件为空或者只有注释！将使用默认配置")
            data = {}
        # print(f"解析到的配置原始数据: {data}")
        return cls(**data)

settings = AppSettings.from_yaml(config_path)