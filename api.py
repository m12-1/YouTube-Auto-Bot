from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
import uuid

# Import modules
from modules.scheduler.scheduler import run as run_scheduler
from modules.script_generation.script_generator import run as run_script_generator
from modules.video_generation.video_generator import run as run_video_generator
from modules.upload.uploader import run as run_uploader
from modules.analytics.tracker import run as run_analytics
from modules.analytics.reporter import run as run_reporter
from modules.maintenance.cleaner import run as run_cleaner

app = FastAPI(title="YouTube Shorts Platform API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GenerateRequest(BaseModel):
    category: Optional[str] = None
    
class PublishRequest(BaseModel):
    video_path: str
    title: str
    description: str
    tags: List[str] = []

class CleanRequest(BaseModel):
    type: str = "temp_files"
    dry_run: bool = False

@app.post("/api/v1/generate")
def generate_pipeline(req: GenerateRequest):
    input_data = {"triggered_by": "api", "category_hint": req.category}
    sched_res = run_scheduler(input_data)
    if sched_res["status"] == "error":
        raise HTTPException(status_code=500, detail=sched_res["error"])
        
    return {"status": "success", "run_id": sched_res["data"]["run_id"], "message": "Pipeline initiated"}

@app.post("/api/v1/publish")
def publish_video(req: PublishRequest):
    input_data = {
        "run_id": str(uuid.uuid4()),
        "video_path": req.video_path,
        "title": req.title,
        "description": req.description,
        "tags": req.tags
    }
    res = run_uploader(input_data)
    if res["status"] == "error":
        raise HTTPException(status_code=500, detail=res["error"])
    return res

@app.get("/api/v1/analytics/{video_id}")
def get_analytics(video_id: str):
    res = run_analytics({"run_id": str(uuid.uuid4()), "video_id": video_id})
    if res["status"] == "error":
        raise HTTPException(status_code=500, detail=res["error"])
    return res

@app.get("/api/v1/reports/{report_type}")
def get_report(report_type: str, format: str = "json"):
    res = run_reporter({"run_id": str(uuid.uuid4()), "report_type": report_type, "format": format, "time_range": "30d"})
    if res["status"] == "error":
        raise HTTPException(status_code=500, detail=res["error"])
    return res

@app.get("/api/v1/health")
def health_check():
    return {"status": "ok"}

@app.get("/api/v1/status")
def system_status():
    return {"status": "system operational"}

@app.get("/api/v1/logs")
def get_logs():
    return {"logs": ["Log entries not directly accessible via simple endpoint yet."]}

@app.post("/api/v1/clean")
def trigger_cleanup(req: CleanRequest):
    res = run_cleaner({"run_id": str(uuid.uuid4()), "clean_type": req.type, "dry_run": req.dry_run, "retention_days": 30})
    if res["status"] == "error":
        raise HTTPException(status_code=500, detail=res["error"])
    return res
