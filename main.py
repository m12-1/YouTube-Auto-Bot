import argparse
import json
import sys
import logging
from typing import Dict, Any

from shared.logger import get_logger

# Import modules
from modules.scheduler.scheduler import run as run_scheduler
from modules.script_generation.script_generator import run as run_script_generator
from modules.video_generation.video_generator import run as run_video_generator
from modules.upload.uploader import run as run_uploader
from modules.analytics.tracker import run as run_analytics
from modules.analytics.reporter import run as run_reporter
from modules.maintenance.cleaner import run as run_cleaner

logger = get_logger("cli")

def pretty_print(result: Dict[str, Any]):
    print(json.dumps(result, indent=2))
    
def check_error(result: Dict[str, Any]):
    if result.get("status") == "error":
        logger.error(f"Module {result.get('module')} failed: {result.get('error')}")
        sys.exit(1)

def run_pipeline(args):
    # Dummy pipeline implementation
    input_data = {"triggered_by": "cli", "category_hint": args.category}
    
    # Scheduler
    sched_res = run_scheduler(input_data)
    check_error(sched_res)
    
    # Script Generation (mock passing data)
    script_input = {"run_id": sched_res["data"]["run_id"], "category": args.category or "tech", "platform": "youtube"}
    script_res = run_script_generator(script_input)
    check_error(script_res)
    
    # Video Generation
    video_input = {"run_id": sched_res["data"]["run_id"], "script_text": script_res["data"]["script_text"], "title": script_res["data"]["title"], "tts_audio_path": "mock.mp3", "assets": []}
    video_res = run_video_generator(video_input)
    check_error(video_res)
    
    # Upload
    upload_input = {"run_id": sched_res["data"]["run_id"], "video_path": video_res["data"]["final_video_path"], "title": video_res["data"]["title"], "description": video_res["data"]["description"], "tags": video_res["data"]["tags"]}
    upload_res = run_uploader(upload_input)
    check_error(upload_res)
    
    pretty_print(upload_res)

def publish_video(args):
    input_data = {"run_id": "cli_publish", "video_path": args.video_path, "title": "Manual Publish", "description": "Published via CLI", "tags": []}
    result = run_uploader(input_data)
    pretty_print(result)
    
def collect_analytics(args):
    input_data = {"run_id": "cli_analytics", "video_id": args.video_id}
    result = run_analytics(input_data)
    pretty_print(result)

def generate_report(args):
    input_data = {"run_id": "cli_report", "report_type": args.type, "format": args.format, "time_range": "30d"}
    result = run_reporter(input_data)
    pretty_print(result)

def cleanup(args):
    input_data = {"run_id": "cli_cleanup", "clean_type": args.type, "dry_run": args.dry_run, "retention_days": 30}
    result = run_cleaner(input_data)
    pretty_print(result)

def show_status(args):
    print(json.dumps({"status": "system operational", "modules_loaded": True}, indent=2))

def run_tests(args):
    print("Running tests... (mock)")

def main():
    parser = argparse.ArgumentParser(description="YouTube Shorts Platform CLI")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    run_parser = subparsers.add_parser("run", help="Run full pipeline")
    run_parser.add_argument("--category", type=str, help="Category hint")
    
    publish_parser = subparsers.add_parser("publish", help="Publish a video")
    publish_parser.add_argument("--video-path", type=str, required=True, help="Path to video file")
    
    analytics_parser = subparsers.add_parser("analytics", help="Collect analytics")
    analytics_parser.add_argument("--video-id", type=str, required=True, help="Video ID")
    
    report_parser = subparsers.add_parser("report", help="Generate report")
    report_parser.add_argument("--type", type=str, default="performance", help="Report type")
    report_parser.add_argument("--format", type=str, default="json", help="Report format")
    
    clean_parser = subparsers.add_parser("clean", help="Cleanup")
    clean_parser.add_argument("--type", type=str, default="temp_files", help="Cleanup type")
    clean_parser.add_argument("--dry-run", action="store_true", help="Dry run mode")
    
    subparsers.add_parser("status", help="Show system status")
    subparsers.add_parser("test", help="Run tests")
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        
    try:
        if args.command == "run":
            run_pipeline(args)
        elif args.command == "publish":
            publish_video(args)
        elif args.command == "analytics":
            collect_analytics(args)
        elif args.command == "report":
            generate_report(args)
        elif args.command == "clean":
            cleanup(args)
        elif args.command == "status":
            show_status(args)
        elif args.command == "test":
            run_tests(args)
    except Exception as e:
        logger.error(f"Command execution failed: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
