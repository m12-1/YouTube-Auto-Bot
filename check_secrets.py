#!/usr/bin/env python3
"""
check_secrets.py

Prints a SAFE report of every secret this project depends on:
- whether it's SET or MISSING
- its length (not its value)
- a short one-way hash fingerprint, so you can tell two secrets apart
  (or spot that two are IDENTICAL / accidentally duplicated) WITHOUT
  ever printing or logging the actual secret value.

Specifically flags the exact problem from the pipeline logs: if
GEMINI_KEY_ADVANCED / _FILTER / _FILTER_2 / _IMAGE / _LIGHT are missing
or all point at the same underlying key, key-rotation in
shared/gemini_client.py has nothing to rotate TO, even though model
rotation is working.

USAGE
-----
Run it directly:
    python check_secrets.py

Or add it as a step in your GitHub Actions workflow, BEFORE the
`python main.py` step, so a missing/misconfigured secret fails fast
with a clear report instead of 40 minutes of silent 429 retries:

    - name: Check secrets
      run: python check_secrets.py
      env:
        GEMINI_KEY_ADVANCED: ${{ secrets.GEMINI_KEY_ADVANCED }}
        GEMINI_KEY_FILTER: ${{ secrets.GEMINI_KEY_FILTER }}
        GEMINI_KEY_FILTER_2: ${{ secrets.GEMINI_KEY_FILTER_2 }}
        GEMINI_KEY_IMAGE: ${{ secrets.GEMINI_KEY_IMAGE }}
        GEMINI_KEY_LIGHT: ${{ secrets.GEMINI_KEY_LIGHT }}
        GH_PAT: ${{ secrets.GH_PAT }}
        GOOGLE_SERVICE_ACCOUNT_JSON: ${{ secrets.GOOGLE_SERVICE_ACCOUNT_JSON }}
        GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
        PEXELS_API_KEY: ${{ secrets.PEXELS_API_KEY }}
        PIXABAY_API_KEY: ${{ secrets.PIXABAY_API_KEY }}
        PUTER_USERNAME: ${{ secrets.PUTER_USERNAME }}
        PUTER_PASSWORD: ${{ secrets.PUTER_PASSWORD }}
        SPREADSHEET_ID: ${{ secrets.SPREADSHEET_ID }}
        TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
        TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        YOUTUBE_OAUTH_CLIENT_ID: ${{ secrets.YOUTUBE_OAUTH_CLIENT_ID }}
        YOUTUBE_OAUTH_CLIENT_SECRET: ${{ secrets.YOUTUBE_OAUTH_CLIENT_SECRET }}
        YOUTUBE_OAUTH_REFRESH_TOKEN: ${{ secrets.YOUTUBE_OAUTH_REFRESH_TOKEN }}
        YOUTUBE_SEARCH_API_KEY: ${{ secrets.YOUTUBE_SEARCH_API_KEY }}

(Your existing `python main.py` step already forwards these same env
vars, per config/settings.py -- just copy the `env:` block from that
step if you have one already, or reuse a single job-level `env:`
block shared by both steps.)

Exit code is 1 if any of the 5 Gemini keys are missing or duplicated
(the case that breaks key-rotation), so this can gate the workflow.
Exit code is always 0 for other missing secrets (they may be
intentionally unused for a given run), but they're still reported.
"""

from __future__ import annotations

import hashlib
import os
import sys

# Secrets that MUST all be present and MUST all be distinct for
# shared/gemini_client.py's key-rotation to actually have somewhere to
# rotate to. Order matches the priority order used across the modules.
GEMINI_KEY_NAMES = [
    "GEMINI_KEY_ADVANCED",
    "GEMINI_KEY_FILTER",
    "GEMINI_KEY_FILTER_2",
    "GEMINI_KEY_IMAGE",
    "GEMINI_KEY_LIGHT",
]

# Every other secret the project reads (config/settings.py ->
# get_all_secret_names()), checked for presence only.
OTHER_SECRET_NAMES = [
    "GH_PAT",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GROQ_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "PUTER_USERNAME",
    "PUTER_PASSWORD",
    "SPREADSHEET_ID",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "YOUTUBE_OAUTH_CLIENT_ID",
    "YOUTUBE_OAUTH_CLIENT_SECRET",
    "YOUTUBE_OAUTH_REFRESH_TOKEN",
    "YOUTUBE_SEARCH_API_KEY",
]


def fingerprint(value: str) -> str:
    """
    A short, one-way fingerprint of a secret's value: enough to tell
    whether two env vars hold the SAME value (duplicate) or DIFFERENT
    values, without ever reconstructing or printing the value itself.
    Not reversible.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def report_row(name: str) -> tuple[str, bool, int, str]:
    """
    Returns (name, is_set, length, fingerprint_or_dash) for one env var.
    Never returns or logs the actual value.
    """
    value = os.environ.get(name)
    if not value:
        return name, False, 0, "-"
    return name, True, len(value), fingerprint(value)


def main() -> int:
    print("=" * 72)
    print("SECRETS HEALTH CHECK (values are never printed or logged)")
    print("=" * 72)

    exit_code = 0

    # --- Gemini keys: presence + duplicate detection ---
    print("\n[Gemini API keys -- required for model+key rotation]")
    print(f"{'NAME':<24} {'STATUS':<10} {'LENGTH':<8} {'FINGERPRINT'}")
    print("-" * 72)

    gemini_rows = [report_row(name) for name in GEMINI_KEY_NAMES]
    seen_fingerprints: dict[str, list[str]] = {}
    set_count = 0

    for name, is_set, length, fp in gemini_rows:
        status = "SET" if is_set else "MISSING"
        print(f"{name:<24} {status:<10} {length:<8} {fp}")
        if is_set:
            set_count += 1
            seen_fingerprints.setdefault(fp, []).append(name)

    missing = [name for name, is_set, _, _ in gemini_rows if not is_set]
    duplicates = {fp: names for fp, names in seen_fingerprints.items() if len(names) > 1}

    print()
    if missing:
        print(f"⚠️  MISSING: {', '.join(missing)}")
        print("   -> key-rotation has fewer keys to fall back to than expected.")
        exit_code = 1
    if duplicates:
        for fp, names in duplicates.items():
            print(f"⚠️  DUPLICATE VALUE across: {', '.join(names)} (fingerprint {fp})")
        print("   -> these secrets are set to the SAME underlying key, so rotating")
        print("      between them does nothing for quota -- fix by assigning each a")
        print("      genuinely different Gemini API key.")
        exit_code = 1
    if not missing and not duplicates:
        print(f"✅ All {set_count}/5 Gemini keys are set and all distinct.")

    # --- Everything else: presence only ---
    print("\n[Other secrets]")
    print(f"{'NAME':<28} {'STATUS':<10} {'LENGTH'}")
    print("-" * 72)
    for name in OTHER_SECRET_NAMES:
        _, is_set, length, _ = report_row(name)
        status = "SET" if is_set else "MISSING"
        print(f"{name:<28} {status:<10} {length}")

    print("\n" + "=" * 72)
    if exit_code:
        print("RESULT: FAIL -- fix the Gemini key issues above before running the pipeline.")
    else:
        print("RESULT: OK")
    print("=" * 72)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
