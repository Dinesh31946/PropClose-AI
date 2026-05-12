"""Pytest bootstrap for the PropClose AI backend test suite.

This file does two things, both of which are pure test-infrastructure
(it never patches application logic):

1. Adds the ``backend/`` directory to ``sys.path`` so the test files can
   ``from app... import ...`` exactly the way uvicorn does in production.
2. Sets harmless placeholder credentials in the process environment so that
   ``Settings.load()`` and ``get_supabase_client()`` succeed at import time
   even on a clean machine (no .env.local, no real keys, no network).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("RAG_SIMILARITY_THRESHOLD", "0.3")
