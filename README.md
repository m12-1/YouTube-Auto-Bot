# YouTube Shorts Platform — Current Phase

Personal, local (later GitHub Actions) YouTube Shorts bot.

**Input:** one topic.
**Output:** one rendered Short (`final.mp4`) plus its knowledge/content/media
JSON artifacts.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values to use live AI/media APIs
python main.py --category science
```

Without any API keys configured, every stage still completes with a
documented fallback; `quality_inspector` will correctly report `FAIL`
in that case, since no real media/render output exists yet to publish.

## Testing

```bash
pytest tests/ -v
```

## Current-phase pipeline (18 stages)

```
scheduler -> topic_selector -> competitor_analyzer -> fact_collector ->
fact_verifier -> script_generator -> script_reviewer -> seo_generator ->
storyboard_generator -> media_planner -> media_downloader ->
media_quality_filter -> ai_media_verification -> voice_generator ->
subtitle_generator -> video_composer -> video_renderer -> quality_inspector
```

Orchestrated by `core/pipeline_controller.py`. See that file's docstring
for how each conceptual stage (Hook Generator, Scene Splitter, Media
Ranking, etc.) maps onto the modules above -- several conceptual stages
share one existing module rather than each getting a dedicated file, to
avoid an architecture rewrite during this cleanup pass.

## Project structure

```
youtube_shorts_platform/
├── config/            # settings.py (secrets), pipeline_config.py (tuning)
├── core/               # pipeline_controller.py -- the orchestrator
├── shared/              # logger, retry, json_contract, path_utils, gemini_client
├── prompts/              # AI prompt templates
├── modules/               # the 18 active current-phase modules
├── future/modules/         # Phase 2 code, disconnected but NOT deleted:
│                            #   topic_memory, analytics_collector,
│                            #   performance_analyzer, learning_engine,
│                            #   knowledge_base, monthly_strategy, publisher,
│                            #   ai_router, cache_manager, cleanup_manager,
│                            #   config_manager, database, monitoring,
│                            #   prompt_manager, report_generator
├── tests/
├── main.py              # CLI entrypoint: python main.py [--category X]
├── .env.example
└── requirements.txt
```

## Architecture rules

- Every module lives in its own file, single responsibility, exposes
  exactly one public function: `run(input_json) -> output_json`.
- Modules never import each other's internals -- only
  `core/pipeline_controller.py` wires modules together, via `run()` only.
- All inter-module communication is plain JSON following
  `shared/json_contract.py`'s envelope:
  ```json
  { "status": "success | error", "module": "<name>", "data": {}, "error": null }
  ```
- No hardcoded values. Secrets live in `config/settings.py`; pipeline
  tuning lives in `config/pipeline_config.py` -- both read exclusively
  from environment variables (see `.env.example`).
- All AI prompt text lives in `prompts/*.txt`, never inline in a module.
- Logging goes through `shared/logger.get_logger(__name__)` everywhere.
- Network calls use `@shared.retry.retry(...)`. An external API failing
  (403/404/timeout/rate-limit/missing key) must never crash the
  pipeline -- the module falls back to a deterministic result and the
  pipeline continues.
- `shared/gemini_client.py` is the single abstract entry point for
  Gemini text/vision calls.

## Phase 2 roadmap (not deleted, just disconnected)

`future/modules/` holds code for: analytics, performance analysis, a
learning engine, a knowledge base, monthly strategy, publishing,
long-term topic memory, an AI provider router, caching, cleanup,
a redundant config manager, a database layer, monitoring, a prompt
manager, and report generation. None of it is wired into
`core/pipeline_controller.py` right now -- it's kept for when the
project grows past "one topic -> one MP4".

No REST API, no FastAPI, no Docker, no SaaS architecture in this phase.
