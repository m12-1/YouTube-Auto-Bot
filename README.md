# YouTube Shorts Platform — Part 2 of 3

Foundation (Part 1) plus the complete content generation engine
(Part 2) of a modular, production-grade AI-powered YouTube Shorts
platform.

## Scope of Part 1 (unchanged)

- `modules/scheduler` — starts a pipeline run, generates `run_id`.
- `modules/topic_selector` — selects a category + topic.
- `modules/competitor_analyzer` — mocked competitor analysis.
- `modules/fact_collector` — placeholder fact collection.
- `modules/fact_verifier` — confidence-based fact verification.

## Scope of Part 2 (new)

Content generation engine, wired onto the end of the Part 1 sequence:

- `modules/script_generator` — hook, question, narration, CTA, scene
  breakdown (Gemini-backed, template fallback).
- `modules/script_reviewer` — grammar/flow/retention/simplicity/
  duplication review, returns an improved script (Gemini-backed,
  heuristic fallback).
- `modules/seo_generator` — title, description, tags, hashtags, CTR
  prediction, SEO score (Gemini-backed, heuristic fallback).
- `modules/storyboard_generator` — converts narration + scene
  breakdown into timed scenes with keywords/animation/transition.
- `modules/media_planner` — turns the storyboard into concrete media
  requirements (type, duration, priority, keywords, camera movement).
- `modules/media_downloader` — searches/downloads media from Pexels
  and Pixabay, with local caching, retries, and progress logging.
- `modules/media_quality_filter` — rejects low-res, wrong-orientation,
  too-dark, blurry, or too-short candidates.
- `modules/ai_media_verification` — scores remaining candidates
  against each scene's narration using an abstracted Gemini Vision
  interface (heuristic keyword-overlap fallback).
- `modules/voice_generator` — Edge-TTS narration with randomized
  voice/speed/pitch and natural pauses; returns word/sentence timings
  (simulated-timing fallback).
- `modules/subtitle_generator` — word-by-word, animated-highlight
  subtitle timeline from word timings.
- `modules/video_composer` — combines storyboard + media + voice +
  subtitles + transitions into a declarative render plan.
- `modules/video_renderer` — renders `final.mp4` / `thumbnail.jpg` /
  `metadata.json` / `seo.json` at 1080x1920 @ 30 FPS via MoviePy, with
  Ken Burns/transition support (manifest-only fallback when media or
  MoviePy is unavailable).
- `modules/quality_inspector` — final PASS/FAIL QA gate (missing
  scenes/subtitles, resolution, duration, audio clipping, render
  failures).

Every AI- or network-backed module in Part 2 follows the same pattern
established in Part 1: attempt the real call, and on any failure
(missing key, no network, bad response) fall back to a deterministic
heuristic so `core/pipeline_controller` always completes end-to-end.

Still scaffolded but **not yet implemented** (placeholders only,
Part 3): `modules/publisher`. The earlier Part 1 stub folders
`modules/script_writer`, `modules/media_search`, and
`modules/narration` are kept in place (unused, untouched) since their
responsibilities were absorbed by the more granular Part 2 modules
above — folder names are never removed or renamed per the
architecture rules.

## Architecture rules

- Every module lives in its own file, with a single responsibility,
  and exposes exactly one public function: `run(input_json) -> output_json`.
- Modules never import each other's internals — only `core/pipeline_controller`
  wires modules together, and only via each module's public `run()`.
- All inter-module communication is plain JSON-serializable dicts,
  following the shared envelope defined in `shared/json_contract.py`:

  ```json
  {
    "status": "success | error",
    "module": "<module_name>",
    "data": { "...": "..." },
    "error": null
  }
  ```

- No hardcoded values. Secrets live in `config/settings.py`;
  non-secret pipeline tuning (resolution, thresholds, cache dirs,
  voice pools, etc.) lives in `config/pipeline_config.py` — both
  read exclusively from environment variables.
- All AI prompt text lives in `prompts/*.txt` templates, never inline
  in a module's `.py` file.
- Logging goes through `shared/logger.get_logger(__name__)` everywhere.
- Flaky/network-style operations use the `@shared.retry.retry(...)`
  decorator instead of ad-hoc retry loops.
- `shared/gemini_client.py` is the single, abstract entry point for
  Gemini text/vision calls, so the underlying model/provider can be
  swapped later without touching any calling module.

## Project structure

```
youtube_shorts_platform/
├── config/
│   ├── settings.py                 # secrets, env-var driven
│   └── pipeline_config.py          # non-secret pipeline tuning (new)
├── core/
│   └── pipeline_controller.py      # orchestrates all 18 stages in sequence
├── shared/
│   ├── logger.py
│   ├── retry.py
│   ├── json_contract.py
│   └── gemini_client.py            # abstract Gemini text/vision wrapper (new)
├── prompts/
│   ├── script_generator_prompt.txt
│   ├── script_reviewer_prompt.txt
│   ├── seo_generator_prompt.txt
│   └── ai_media_verification_prompt.txt
├── modules/
│   ├── scheduler/
│   ├── topic_selector/
│   ├── competitor_analyzer/
│   ├── fact_collector/
│   ├── fact_verifier/
│   ├── script_generator/           # new
│   ├── script_reviewer/            # new
│   ├── seo_generator/              # new
│   ├── storyboard_generator/       # new
│   ├── media_planner/              # new
│   ├── media_downloader/           # new
│   ├── media_quality_filter/       # new
│   ├── ai_media_verification/      # new
│   ├── voice_generator/            # new
│   ├── subtitle_generator/         # new
│   ├── video_composer/             # new
│   ├── video_renderer/             # replaces Part 1 stub
│   ├── script_writer/              # unused Part 1 stub, kept as-is
│   ├── media_search/                # unused Part 1 stub, kept as-is
│   ├── narration/                   # unused Part 1 stub, kept as-is
│   └── publisher/                   # still a stub (Part 3)
├── tests/
│   ├── test_pipeline_controller.py
│   └── test_part2_modules.py       # new
├── .env.example
├── requirements.txt
└── README.md
```

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in real values to use live AI/media APIs
python -c "from core.pipeline_controller import run; import json; print(json.dumps(run({'category_hint': 'science'}), indent=2, ensure_ascii=False))"
```

Without any API keys configured, every stage still completes
successfully using its documented fallback path; `quality_inspector`
will correctly report `FAIL` in that case, since no real media/render
output exists yet to publish.

## Testing

```bash
pytest tests/ -v
```

## Secrets inventory

`config/settings.py` centralizes reads for the following environment
variables (see `.env.example`): `GEMINI_KEY_ADVANCED`,
`GEMINI_KEY_FILTER`, `GEMINI_KEY_FILTER_2`, `GEMINI_KEY_IMAGE`,
`GEMINI_KEY_LIGHT`, `GH_PAT`, `GOOGLE_SERVICE_ACCOUNT_JSON`,
`GROQ_API_KEY`, `PEXELS_API_KEY`, `PIXABAY_API_KEY`,
`PUTER_USERNAME`, `PUTER_PASSWORD`, `SPREADSHEET_ID`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `YOUTUBE_OAUTH_CLIENT_ID`,
`YOUTUBE_OAUTH_CLIENT_SECRET`, `YOUTUBE_OAUTH_REFRESH_TOKEN`,
`YOUTUBE_SEARCH_API_KEY`.

This exact secret name list matches the platform's existing inventory
and was not modified in Part 2. Part 2 modules use: `GEMINI_KEY_ADVANCED`
/ `GEMINI_KEY_LIGHT` (script_generator, script_reviewer),
`GEMINI_KEY_FILTER` / `GEMINI_KEY_FILTER_2` / `GEMINI_KEY_LIGHT`
(seo_generator), `GEMINI_KEY_IMAGE` / `GEMINI_KEY_ADVANCED`
(ai_media_verification), and `PEXELS_API_KEY` / `PIXABAY_API_KEY`
(media_downloader).

## Part 3 preview

`modules/publisher` plus analytics, a learning engine, a knowledge
base, AI router improvements, scheduling enhancements, monitoring,
reporting, and production deployment will be implemented in Part 3.
