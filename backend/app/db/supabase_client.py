from functools import lru_cache

from supabase import Client, create_client

from app.core.config import Settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.load()


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError(
            "Supabase credentials are missing. Set SUPABASE_URL "
            "and SUPABASE_SERVICE_ROLE_KEY in environment or .env.local."
        )
    return create_client(settings.supabase_url, settings.supabase_service_role_key)

