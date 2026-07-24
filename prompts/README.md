# prompts/

Versioned prompt template files consumed by AI-backed modules.
Templates are plain `.txt` files with `{placeholder}` tokens filled in
via `str.format()` by the calling module — no prompt text is ever
hardcoded inside a module's `.py` file.

Currently used by (Part 2):
- `script_generator_prompt.txt` — modules/script_generator
- `script_reviewer_prompt.txt` — modules/script_reviewer
- `seo_generator_prompt.txt` — modules/seo_generator
- `ai_media_verification_prompt.txt` — modules/ai_media_verification

Each module falls back to a deterministic heuristic implementation
when no Gemini API key is configured or the API call fails, so the
pipeline keeps working end-to-end without live credentials.

Additional prompts (publishing captions, analytics summaries, etc.)
will be added in Part 3.
