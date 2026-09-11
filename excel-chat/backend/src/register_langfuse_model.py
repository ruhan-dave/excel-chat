#!/usr/bin/env python3
"""Register LLM model pricing in Langfuse so GENERATION observations
auto-calculate cost.

Langfuse only shows cost in the UI cost column / dashboards when it has
pricing data for the model.  This script registers the model(s) used by
the excel-chat backend in the Langfuse project bound to your API keys.

Usage:
    .venv/bin/python backend/src/register_langfuse_model.py

Requires LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in .env (or environment).

Idempotent: if the model already exists with the same pricing, Langfuse
returns the existing record (no duplicate).  To update pricing, delete
the model in the Langfuse UI first, or change the match pattern.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add backend/src to path so this runs standalone
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

# Load .env from project root (two levels up from backend/src)
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


# ---------------------------------------------------------------------------
# Model pricing registry
# ---------------------------------------------------------------------------
# Prices are per-token (USD).  To convert from per-1M-token pricing:
#   per_token = per_mtok / 1_000_000
#
# Source: https://openrouter.ai/deepseek/deepseek-v4-flash (genai-prices)
# Verified against pydantic-ai's genai_prices.calc_price() output.
# ---------------------------------------------------------------------------

MODELS = [
    # --- Active models (uncomment to register) ---
    {
        "model_name": "deepseek/deepseek-v4-flash",
        "match_pattern": r"(?i)^(deepseek/)?deepseek-v4-flash",
        "input_price": 9.83e-8,   # $0.0983/M tokens
        "output_price": 1.966e-7, # $0.1966/M tokens
    },

    # --- Other models (commented out — uncomment to register) ---
    # {
    #     "model_name": "openai/gpt-oss-120b:nitro",
    #     "match_pattern": r"(?i)^(openai/)?gpt-oss-120b(:nitro)?$",
    #     "input_price": 1.5e-7,   # $0.15/M tokens
    #     "output_price": 7.5e-7,  # $0.75/M tokens
    # },
]


def main() -> int:
    required = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: {', '.join(missing)} not set in .env or environment")
        return 1

    from langfuse import get_client

    client = get_client()
    if not client.auth_check():
        print("ERROR: Langfuse auth_check failed — verify your API keys")
        return 1

    print(f"Connected to Langfuse: {os.environ.get('LANGFUSE_BASE_URL', 'https://cloud.langfuse.com')}")
    print()

    # List existing models to avoid duplicates
    existing = client.api.models.list(limit=100)
    existing_models = existing.data if hasattr(existing, "data") else existing
    existing_names = {m.model_name for m in existing_models}
    existing_patterns = {m.match_pattern for m in existing_models}

    registered = 0
    skipped = 0
    for model in MODELS:
        name = model["model_name"]
        pattern = model["match_pattern"]

        if name in existing_names or pattern in existing_patterns:
            print(f"  SKIP  {name} (already registered)")
            skipped += 1
            continue

        try:
            result = client.api.models.create(
                model_name=name,
                match_pattern=pattern,
                unit="TOKENS",
                input_price=model["input_price"],
                output_price=model["output_price"],
            )
            print(
                f"  OK   {name} (id={result.id})\n"
                f"       input:  ${model['input_price'] * 1e6:.4f}/M tokens\n"
                f"       output: ${model['output_price'] * 1e6:.4f}/M tokens"
            )
            registered += 1
        except Exception as exc:
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")

    print()
    print(f"Done: {registered} registered, {skipped} skipped, {len(MODELS)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
