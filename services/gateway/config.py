"""Gateway configuration. Environment-driven; sensible compose defaults."""
import os


class Settings:
    service_name = "gateway"
    environment = os.getenv("ENVIRONMENT", "development")
    log_level = os.getenv("LOG_LEVEL", "INFO")

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")

    db_host = os.getenv("DB_HOST", "postgres")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_NAME", "riverbend")
    db_user = os.getenv("DB_USER", "riverbend_app")
    db_password = os.getenv("DB_PASSWORD", "")

    # downstream services
    intake_url = os.getenv("INTAKE_URL", "http://intake-service:8071")
    eligibility_url = os.getenv("ELIGIBILITY_URL", "http://eligibility-service:8072")
    records_url = os.getenv("RECORDS_URL", "http://records-service:8073")
    scheduling_url = os.getenv("SCHEDULING_URL", "http://scheduling-service:8074")
    interop_url = os.getenv("INTEROP_URL", "http://interop-service:8075")
    roi_url = os.getenv("ROI_URL", "http://roi-service:8076")
    ai_orchestrator_url = os.getenv("AI_ORCHESTRATOR_URL", "http://ai-orchestrator:8077")

    # W2 — knowledge-base ingest capability (see authz.py). Interim control until
    # the role-hierarchy migration (D7, W9); the env allowlist exists because the
    # inherited system has exactly one role and cannot express a capability.
    knowledge_ingest_users = frozenset(
        u.strip() for u in os.getenv("KNOWLEDGE_INGEST_USERS", "").split(",") if u.strip()
    )
    knowledge_ingest_roles = frozenset(
        r.strip() for r in os.getenv("KNOWLEDGE_INGEST_ROLES", "knowledge_admin").split(",")
        if r.strip()
    )

    @property
    def db_url(self) -> str:
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


settings = Settings()
