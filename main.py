"""
main.py

CLI entrypoint for the YouTube Shorts pipeline.

Local usage:
    python main.py
    python main.py --category science
    python main.py --topic "Black holes explained"
    python main.py --topic "Black holes" --privacy unlisted
    python main.py --dry-run          # render everything, skip YouTube upload

GitHub Actions usage:
    Triggered via workflow_dispatch in .github/workflows/run_pipeline.yml.
    Inputs (topic, category, privacy, dry-run) are forwarded here as
    command-line arguments by the workflow.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from core.pipeline_controller import run as run_pipeline
from shared.logger import get_logger

logger = get_logger("main")

PRIVACY_CHOICES = ("private", "unlisted", "public")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the YouTube Shorts pipeline for one topic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
privacy options:
  private   Only you can see the video (default — safest for testing)
  unlisted  Anyone with the link can watch; not searchable
  public    Visible to everyone on YouTube
        """,
    )

    parser.add_argument(
        "--topic",
        type=str,
        default=None,
        help=(
            "Exact topic to generate a Short about. "
            "When omitted the Topic Selector module picks one automatically "
            "based on --category."
        ),
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        help="Category hint for the Topic Selector (e.g. science, technology, history).",
    )
    parser.add_argument(
        "--privacy",
        type=str,
        choices=PRIVACY_CHOICES,
        default="private",
        help=(
            "YouTube video privacy after upload. "
            "Default: private (safe for testing — change to public only when ready)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Render the video but skip the YouTube upload step. "
            "Useful for testing the full pipeline without publishing."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Log what we're about to do so runs are easy to understand in
    # CI logs and in local terminals.
    mode = "DRY RUN" if args.dry_run else f"PUBLISH ({args.privacy.upper()})"
    if args.topic:
        logger.info("Starting pipeline | topic='%s' | mode=%s", args.topic, mode)
    else:
        logger.info(
            "Starting pipeline | category='%s' (topic auto-selected) | mode=%s",
            args.category or "auto",
            mode,
        )

    input_data: dict = {
        "triggered_by": "cli",
        "category_hint": args.category,
        # topic_selector passes this down if provided — when None it picks its own
        "forced_topic": args.topic,
        # privacy is forwarded all the way through to the publisher module
        "privacy": args.privacy,
        # dry_run tells the publisher to skip the actual upload
        "dry_run": args.dry_run,
    }

    result = run_pipeline(input_data)

    print(json.dumps(result, indent=2, ensure_ascii=False))

    if result.get("status") != "success":
        failed = result.get("data", {}).get("failed_stage", "unknown")
        logger.error(
            "Pipeline failed at stage '%s': %s",
            failed,
            result.get("error"),
        )
        sys.exit(1)

    # A stage reporting status="success" only means it ran without a
    # technical error -- it does NOT mean the video is fit to publish.
    # quality_inspector reports its PASS/FAIL business verdict inside its
    # own data, not through the envelope status (see quality_inspector.py),
    # so that verdict must be checked here explicitly. Without this check,
    # a run with missing media or a manifest-only render would print
    # "Pipeline finished successfully" and exit 0 even though no usable
    # video was produced.
    quality_stage = result.get("data", {}).get("stages", {}).get("quality_inspector", {})
    quality_data = quality_stage.get("data", {})
    verdict = quality_data.get("verdict")

    if verdict != "PASS":
        logger.error(
            "Pipeline completed all stages but the video failed the final "
            "quality gate (verdict=%s): %s",
            verdict,
            "; ".join(quality_data.get("failure_reasons", [])) or "unknown reason",
        )
        sys.exit(1)

    if args.dry_run:
        logger.info(
            "Pipeline finished successfully (dry run -- video rendered but not uploaded)."
        )
        return

    publisher_stage = result.get("data", {}).get("stages", {}).get("publisher", {})
    publisher_data = publisher_stage.get("data", {})
    upload_status = publisher_data.get("upload_status")

    if upload_status == "simulated":
        logger.warning(
            "Pipeline finished, but the video was NOT actually uploaded to "
            "YouTube -- publisher fell back to a simulated upload because "
            "YouTube OAuth credentials are missing or incomplete "
            "(YOUTUBE_OAUTH_CLIENT_ID / YOUTUBE_OAUTH_CLIENT_SECRET / "
            "YOUTUBE_OAUTH_REFRESH_TOKEN). Simulated id: %s",
            publisher_data.get("video_id"),
        )
    elif upload_status == "published":
        logger.info(
            "Pipeline finished successfully. Video published: %s",
            publisher_data.get("video_url"),
        )
    else:
        logger.info("Pipeline finished successfully.")



if __name__ == "__main__":
    main()
