"""Application settings loaded from .env file."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql+asyncpg://user:password@localhost:5432/jurisbound"
    SYNC_DATABASE_URL: str = "postgresql://user:password@localhost:5432/jurisbound"

    # MinIO
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET: str = "court-documents"
    MINIO_SECURE: bool = False

    # CAPTCHA
    TWOCAPTCHA_API_KEY: str = ""
    ANTICAPTCHA_API_KEY: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Qdrant
    QDRANT_HOST: str = "localhost"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "court_docs"

    # API
    SECRET_KEY: str = "change-this-to-a-random-secret-key"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # Scraping
    CONCURRENT_SCRAPERS: int = 5
    REQUEST_DELAY_MIN: float = 1.5
    REQUEST_DELAY_MAX: float = 4.0
    USE_PROXY: bool = False
    PROXY_URL: str = ""

    # (Celery + Redis dropped in v2 — scrapes run from the CLI, no broker needed.)

    # LLM — primary: Cerebras (1M tokens/day free tier, OpenAI-compatible)
    CEREBRAS_ENDPOINT: str = "https://api.cerebras.ai/v1/chat/completions"
    CEREBRAS_API_KEY: str = ""
    CEREBRAS_MODEL: str = "llama3.1-8b"

    # LLM — fallback: Groq (500K tokens/day free tier, OpenAI-compatible).
    # Also covers the v1 LLM_* vars so legacy code paths keep working.
    LLM_ENDPOINT: str = "https://api.groq.com/openai/v1/chat/completions"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "llama-3.1-8b-instant"

    # Cross-encoder reranker — adds ~150ms but big quality lift.
    # Set false to disable.
    JB_RERANK_ENABLED: bool = True
    JB_RERANK_MODEL: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    JB_RERANK_TOP_K: int = 50  # candidates passed to reranker; final result is sliced from these

    # Embedding model upgrade target (used by indexer.py once we switch).
    # arctic-embed-m: 86M params, ~340 MB, 768-dim, beats BGE-large at 1/4 the size.
    EMBED_MODEL: str = "Snowflake/snowflake-arctic-embed-m"
    EMBED_DIM: int = 768

    # Supabase (Phase B)
    SUPABASE_URL: str = ""
    SUPABASE_PROJECT_REF: str = ""
    SUPABASE_ANON_KEY: str = ""              # public, used for read URLs only
    SUPABASE_SERVICE_ROLE_KEY: str = ""      # server-only, bypasses RLS
    SUPABASE_S3_ENDPOINT: str = ""
    SUPABASE_S3_REGION: str = "ap-south-1"
    SUPABASE_S3_ACCESS_KEY: str = ""
    SUPABASE_S3_SECRET_KEY: str = ""
    SUPABASE_S3_BUCKET: str = "court-documents"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # don't crash on stray env vars from local stack
