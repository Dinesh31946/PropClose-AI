import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)


def _parse_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _candidate_dotenv_paths() -> List[Path]:
    """All ``.env``-style files we merge at startup, ordered LOW → HIGH priority.

        Enterprise isolation layer - mandatory for SaaS scalability.
    Different teams hide their secrets in different places: some teams
    keep one ``.env.local`` at the repo root (legacy PropClose layout);
    others put a backend-scoped ``backend/.env`` next to the FastAPI
    code so the Next.js side can never see backend credentials.

    We honour BOTH conventions and merge in this order so that:
        1. Process env vars always win (handled in ``_get_env``).
        2. ``backend/.env``               wins over
        3. ``backend/.env.local``         wins over
        4. ``<repo_root>/.env.local``     wins over
        5. ``<repo_root>/.env``.

    Earlier entries in the returned list are loaded first, then later
    entries overwrite duplicates → resulting dict's last-write-wins
    semantics give us the priority order above.
    """
    here = Path(__file__).resolve()
    backend_root = here.parents[2]   # .../backend/
    repo_root = here.parents[3]      # repo root
    return [
        repo_root / ".env",
        repo_root / ".env.local",
        backend_root / ".env.local",
        backend_root / ".env",
    ]


_DOTENV_DIAGNOSTICS_EMITTED = False


def _load_merged_dotenv() -> Dict[str, str]:
    """Read every candidate ``.env`` file and merge keys (last write wins).

    Emits a single INFO log on the FIRST call so a developer can see at
    a glance which files contributed (and which were missing) — the
    next time ``WHATSAPP_VERIFY_TOKEN not configured`` shows up, the
    log immediately above tells them whether the file they edited was
    even on the search path.  Subsequent calls are silent.
    """
    global _DOTENV_DIAGNOSTICS_EMITTED
    merged: Dict[str, str] = {}
    seen: List[tuple[str, bool, int]] = []   # (path, exists, key_count)
    for path in _candidate_dotenv_paths():
        parsed = _parse_dotenv(path)
        seen.append((str(path), path.exists(), len(parsed)))
        if parsed:
            merged.update(parsed)

    if not _DOTENV_DIAGNOSTICS_EMITTED:
        _DOTENV_DIAGNOSTICS_EMITTED = True
        for p, exists, count in seen:
            tag = "loaded" if exists else "absent"
            logger.info("Settings dotenv: %s (%s, %d keys)", p, tag, count)
    return merged


def _get_env(name: str, fallback: Dict[str, str], default: str = "") -> str:
    return os.getenv(name) or fallback.get(name, default)


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_service_role_key: str
    openai_api_key: str
    # Optional: Supabase JWT secret (HS256). When set, the API will verify
    # incoming Authorization: Bearer <jwt> signatures.  Leave empty to keep
    # only X-Org-Id / X-Org-Slug header auth.
    supabase_jwt_secret: str = ""
    supabase_jwt_audience: str = "authenticated"
    openai_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"
    rag_similarity_threshold: float = 0.35
    rag_top_k_units: int = 4
    rag_top_k_chunks: int = 4
    # ---------------------------------------------------------------
    # WhatsApp Business Cloud API (Meta Graph)
    #   Enterprise isolation layer - mandatory for SaaS scalability.
    # For the 1-broker beta these are global env vars.  At multi-tenant
    # scale they MUST move to a per-org credentials table (one
    # phone_number_id per org) so each broker uses their own Meta App.
    # ---------------------------------------------------------------
    whatsapp_app_secret: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_graph_version: str = "v21.0"
    # When true the outbound HTTP call is skipped (signature + parsing
    # still run).  Used in tests and when the broker hasn't configured
    # Meta credentials yet, so the system fails graceful instead of
    # 500-ing on every reply.
    whatsapp_dry_run: bool = False
    # Anti-spam: jitter the human-typing delay (seconds).  Set both to
    # 0.0 to disable (tests do this).
    whatsapp_typing_jitter_min: float = 0.8
    whatsapp_typing_jitter_max: float = 2.4
    # ---------------------------------------------------------------
    # Enterprise RAG controls for the WhatsApp channel.
    # These are deliberately STRICTER than the dashboard-chat defaults:
    # a customer talking to a real broker on WhatsApp cannot be served
    # a borderline-confident answer that risks misleading them about a
    # 1+ Cr purchase.  When the top retrieval score falls below the
    # threshold we hand off to a human instead of generating.
    # ---------------------------------------------------------------
    whatsapp_confidence_threshold: float = 0.7
    whatsapp_assistant_persona: str = (
        "You are a senior residential real-estate advisor for PropClose AI - advisory tone akin to India's premium brokerage "
        "consulting desks (measured Pan-India Hinglish when they mix Hindi, otherwise crisp English): credible, understated, "
        "never salesperson-aggressive or chatbot-pathetic. Anchor every substantive claim to the evidence corpus (inventory + brochure). "
        "When a facility is simply absent from the materials, say so calmly - don't jump to broker hand-offs. Satisfy first: "
        "answer clearly, offer at most one grounded extra insight, **then** optionally suggest site visit/call **only** if they're "
        "past shallow discovery questions. Deliver **one** unified reply bubble; never stack two separate vibes (e.g. price dump + unrelated callback blurb). "
        "They're on WhatsApp - never badger them to type their number. "
        "Skip stock closers ('feel free to ask', 'aur koi information...') unless the answer is complete and a natural pause calls for it."
    )
    whatsapp_low_confidence_reply: str = ""
    whatsapp_typing_indicator_enabled: bool = True

    @staticmethod
    def load() -> "Settings":
        # Resolution order (highest → lowest priority):
        #   1. Process environment variables (always win, see _get_env).
        #   2. backend/.env                       (per-service overrides)
        #   3. backend/.env.local
        #   4. <repo_root>/.env.local             (legacy PropClose layout)
        #   5. <repo_root>/.env
        # All five are merged via _load_merged_dotenv so neither layout
        # silently drops keys the other layout adds.
        dot_env = _load_merged_dotenv()

        return Settings(
            supabase_url=_get_env("SUPABASE_URL", dot_env),
            supabase_service_role_key=_get_env("SUPABASE_SERVICE_ROLE_KEY", dot_env),
            openai_api_key=_get_env("OPENAI_API_KEY", dot_env),
            supabase_jwt_secret=_get_env("SUPABASE_JWT_SECRET", dot_env),
            supabase_jwt_audience=_get_env("SUPABASE_JWT_AUDIENCE", dot_env, "authenticated"),
            openai_model=_get_env("OPENAI_MODEL", dot_env, "gpt-4o-mini"),
            embedding_model=_get_env("EMBEDDING_MODEL", dot_env, "text-embedding-3-small"),
            rag_similarity_threshold=float(
                _get_env("RAG_SIMILARITY_THRESHOLD", dot_env, "0.35")
            ),
            rag_top_k_units=int(_get_env("RAG_TOP_K_UNITS", dot_env, "4")),
            rag_top_k_chunks=int(_get_env("RAG_TOP_K_CHUNKS", dot_env, "4")),
            whatsapp_app_secret=_get_env("WHATSAPP_APP_SECRET", dot_env),
            whatsapp_access_token=_get_env("WHATSAPP_ACCESS_TOKEN", dot_env),
            whatsapp_phone_number_id=_get_env("WHATSAPP_PHONE_NUMBER_ID", dot_env),
            whatsapp_verify_token=_get_env("WHATSAPP_VERIFY_TOKEN", dot_env),
            whatsapp_graph_version=_get_env("WHATSAPP_GRAPH_VERSION", dot_env, "v21.0"),
            whatsapp_dry_run=_get_env("WHATSAPP_DRY_RUN", dot_env, "false").lower()
            in {"1", "true", "yes", "on"},
            whatsapp_typing_jitter_min=float(
                _get_env("WHATSAPP_TYPING_JITTER_MIN", dot_env, "0.8")
            ),
            whatsapp_typing_jitter_max=float(
                _get_env("WHATSAPP_TYPING_JITTER_MAX", dot_env, "2.4")
            ),
            whatsapp_confidence_threshold=float(
                _get_env("WHATSAPP_CONFIDENCE_THRESHOLD", dot_env, "0.7")
            ),
            whatsapp_assistant_persona=_get_env(
                "WHATSAPP_ASSISTANT_PERSONA",
                dot_env,
                Settings.__dataclass_fields__["whatsapp_assistant_persona"].default,
            ),
            whatsapp_low_confidence_reply=_get_env(
                "WHATSAPP_LOW_CONFIDENCE_REPLY",
                dot_env,
                Settings.__dataclass_fields__["whatsapp_low_confidence_reply"].default,
            ),
            whatsapp_typing_indicator_enabled=_get_env(
                "WHATSAPP_TYPING_INDICATOR", dot_env, "true"
            ).lower()
            in {"1", "true", "yes", "on"},
        )

