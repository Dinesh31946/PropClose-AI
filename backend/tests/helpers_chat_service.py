"""Shared fixtures for patching ``ChatService`` built via ``__new__``."""

from __future__ import annotations

from typing import Any

from unittest.mock import MagicMock

from app.core.config import Settings
from app.services.profiling_service import ProfilingService


def attach_chat_service_profiling_stub(service: Any) -> MagicMock:
    """Stub profiling so ``extract_signals`` does not hit OpenAI; merge/priority stays real."""

    real = ProfilingService(
        Settings(
            supabase_url="x",
            supabase_service_role_key="x",
            openai_api_key="x",
        )
    )
    profiling = MagicMock()
    profiling.extract_signals.return_value = {}
    profiling.merge_into_profile.side_effect = lambda ep, xt: real.merge_into_profile(ep, xt)
    profiling.select_next_missing_key.side_effect = lambda pd: real.select_next_missing_key(pd)
    service.profiling = profiling
    return profiling
