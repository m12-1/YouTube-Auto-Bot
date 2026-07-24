"""
main.py

Minimal CLI entrypoint for the current phase:

    python main.py
    python main.py --category science

Runs the full pipeline (core.pipeline_controller) for one topic and
prints the resulting JSON summary. No REST API, no Docker, no
subcommands beyond this -- current phase scope is one topic in, one
rendered MP4 out.
"""

from __future__ import annotations

import argparse
import json
import sys

from core.pipeline_controller import run as run_pipeline
from shared.logger import get_logger

logger = get_logger("main")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the YouTube Shorts pipeline for one topic."
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Optional category hint for topic selection.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable verbose logging."
    )
    args = parser.parse_args()

    if args.verbose:
        import logging

        logging.getLogger().setLevel(logging.DEBUG)

    input_data = {"triggered_by": "cli", "category_hint": args.category}

    result = run_pipeline(input_data)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result.get("status") != "success":
        logger.error(
            "Pipeline failed at stage '%s': %s",
            result.get("data", {}).get("failed_stage"),
            result.get("error"),
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
