"""
modules/voice_generator/voice_generator.py

Generates narration audio using Edge-TTS, randomizing voice, speed,
pitch, and natural pauses between sentences. Returns the audio file
path plus word-level and sentence-level timings.

If the `edge-tts` package or its network endpoint is unavailable, a
deterministic simulated timing (~2.5 words/second, matching the same
assumption used by `storyboard_generator`) is produced instead so the
rest of the pipeline (subtitles, composition) can still be exercised
end-to-end without live TTS access.

Public contract:
    run(input_json) -> output_json

input_json (required keys):
    {
        "run_id": str,
        "topic": str,
        "script": {"narration": str, ...}
    }

output_json:
    {
        "status": "success" | "error",
        "module": "voice_generator",
        "data": {
            "run_id": str,
            "topic": str,
            "audio_path": str,
            "voice": str,
            "speed": str,
            "pitch": str,
            "word_timings": [ {"word": str, "start": float, "end": float}, ... ],
            "sentence_timings": [ {"sentence": str, "start": float, "end": float}, ... ],
            "source": "edge_tts" | "simulated_fallback"
        },
        "error": str | null
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Tuple

from config import pipeline_config
from shared.json_contract import ContractError, build_response, require_keys
from shared.logger import get_logger
from shared.retry import retry
from shared.path_utils import safe_path, sanitize_filename

logger = get_logger(__name__)

MODULE_NAME = "voice_generator"

try:
    import edge_tts

    _EDGE_TTS_AVAILABLE = True
except ImportError:  # pragma: no cover - environment without edge-tts installed
    _EDGE_TTS_AVAILABLE = False

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


class VoiceGenerationError(RuntimeError):
    """Raised when Edge-TTS synthesis cannot be completed."""


def _split_sentences(narration: str) -> List[str]:
    """Split narration text into sentences on standard terminal punctuation."""
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", narration) if s.strip()]


def _audio_output_path(run_id: str) -> str:
    """Build a stable output path for this run's narration audio file."""
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    safe_digest = sanitize_filename(digest)
    safe_target = safe_path(pipeline_config.VOICE_AUDIO_OUTPUT_DIR, f"narration_{safe_digest}.mp3")
    return str(safe_target)


def _rate_to_speed_multiplier(rate: str) -> float:
    """
    Convert an Edge-TTS style rate string (e.g. "+10%", "-15%", "+0%")
    into a speed multiplier (e.g. 1.10, 0.85, 1.0).

    The simulated fallback previously ignored this value entirely and
    always assumed a fixed 2.5 words/second, even though `voice_generator`
    had already randomly chosen a specific rate for this run. If real
    TTS failed and the simulated timings kicked in, that mismatch meant
    subtitles/scenes were paced for a "neutral" voice speed that was
    never actually requested, silently drifting out of sync with the
    speed the rest of the run (voice/pitch metadata) claims to use.

    Args:
        rate: A percentage string such as "+10%", "-5%", or "+0%".
            Any value that can't be parsed falls back to 1.0 (neutral).

    Returns:
        A positive float multiplier: >1.0 means faster, <1.0 means slower.
    """
    try:
        cleaned = rate.strip().replace("%", "")
        percent = float(cleaned)
    except (ValueError, AttributeError):
        return 1.0
    multiplier = 1.0 + (percent / 100.0)
    # Guard against a pathological/misconfigured rate string collapsing
    # or inverting timing (e.g. "-100%" or lower).
    return max(0.25, multiplier)


def _simulate_timings(
    narration: str, pause_ms: int, rate: str = "+0%"
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Deterministically simulate word- and sentence-level timings when
    real TTS word-boundary data isn't available.

    Args:
        narration: Full narration text.
        pause_ms: Natural pause inserted between sentences, in ms.
        rate: The Edge-TTS rate string that was chosen for this run
            (e.g. "+10%"). The base 2.5 words/second assumption is
            scaled by this so a faster/slower chosen voice speed is
            still reflected in the fallback timings instead of always
            simulating a neutral-speed voice.

    Returns:
        A tuple of (word_timings, sentence_timings).
    """
    base_words_per_second = 2.5
    words_per_second = base_words_per_second * _rate_to_speed_multiplier(rate)
    seconds_per_word = 1.0 / words_per_second
    pause_seconds = pause_ms / 1000.0

    sentences = _split_sentences(narration)
    word_timings: List[Dict[str, Any]] = []
    sentence_timings: List[Dict[str, Any]] = []
    cursor = 0.0

    for sentence in sentences:
        sentence_start = cursor
        for word in sentence.split():
            word_start = round(cursor, 3)
            word_end = round(cursor + seconds_per_word, 3)
            word_timings.append({"word": word, "start": word_start, "end": word_end})
            cursor = word_end
        sentence_timings.append(
            {"sentence": sentence, "start": round(sentence_start, 3), "end": round(cursor, 3)}
        )
        cursor += pause_seconds

    return word_timings, sentence_timings


async def _synthesize_with_edge_tts(
    narration: str, voice: str, rate: str, pitch: str, output_path: str
) -> List[Dict[str, Any]]:
    """
    Run Edge-TTS synthesis, capturing word-boundary timing events.

    Args:
        narration: Full narration text to synthesize.
        voice: Edge-TTS voice name.
        rate: Speed adjustment, e.g. "+10%".
        pitch: Pitch adjustment, e.g. "+2Hz".
        output_path: Where to write the resulting audio file.

    Returns:
        A list of word timing dicts derived from Edge-TTS WordBoundary events.

    Raises:
        VoiceGenerationError: If synthesis produces no audio or word boundaries.
    """
    from pathlib import Path
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    communicate = edge_tts.Communicate(narration, voice=voice, rate=rate, pitch=pitch)
    word_timings: List[Dict[str, Any]] = []

    with open(output_path, "wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_timings.append(
                    {
                        "word": chunk.get("text", ""),
                        "start": round(chunk.get("offset", 0) / 1e7, 3),
                        "end": round((chunk.get("offset", 0) + chunk.get("duration", 0)) / 1e7, 3),
                    }
                )

    if not word_timings:
        raise VoiceGenerationError("Edge-TTS produced no word-boundary timing events.")

    return word_timings


@retry(max_attempts=2, exceptions=(VoiceGenerationError,))
def _generate_with_edge_tts(
    narration: str, voice: str, rate: str, pitch: str, output_path: str
) -> List[Dict[str, Any]]:
    """
    Synchronous wrapper around the async Edge-TTS synthesis call.

    Args:
        narration: Full narration text to synthesize.
        voice: Edge-TTS voice name.
        rate: Speed adjustment string.
        pitch: Pitch adjustment string.
        output_path: Where to write the resulting audio file.

    Returns:
        A list of word timing dicts.

    Raises:
        VoiceGenerationError: If Edge-TTS is unavailable or synthesis fails.
    """
    if not _EDGE_TTS_AVAILABLE:
        raise VoiceGenerationError("edge-tts package is not installed.")

    try:
        return asyncio.run(_synthesize_with_edge_tts(narration, voice, rate, pitch, output_path))
    except VoiceGenerationError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise VoiceGenerationError(str(exc)) from exc


def _normalize_loudness(audio_path: str, target_lufs: float) -> bool:
    """
    Normalize a narration audio file to a consistent integrated loudness
    using ffmpeg's `loudnorm` filter (single-pass), so every run's
    narration lands at roughly the same perceived volume regardless of
    which random voice/rate/pitch combination was picked.

    Without this step, different runs (different voices, different
    natural recording levels from Edge-TTS) can end up at noticeably
    different volumes, which is jarring across a channel's video history
    and can trigger YouTube's own playback normalization to turn a video
    down (wasting headroom) or leave a quiet run sounding weak next to
    louder ones.

    This never raises: if ffmpeg is unavailable or the filter fails for
    any reason, the original file is left untouched and the run
    continues -- a slightly inconsistent volume is far better than a
    failed pipeline run over a cosmetic audio-quality step.

    Args:
        audio_path: Path to the narration audio file to normalize in place.
        target_lufs: Target integrated loudness in LUFS (negative number,
            e.g. -14.0).

    Returns:
        True if normalization was applied, False if it was skipped or failed.
    """
    if not _FFMPEG_AVAILABLE:
        logger.info("Skipping loudness normalization: ffmpeg not found on PATH.")
        return False

    if not os.path.isfile(audio_path):
        return False

    fd, temp_path = tempfile.mkstemp(suffix=os.path.splitext(audio_path)[1] or ".mp3")
    os.close(fd)

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", audio_path,
                "-af", f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11",
                "-ar", "44100",
                temp_path,
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0 or not os.path.isfile(temp_path) or os.path.getsize(temp_path) == 0:
            logger.warning(
                "Loudness normalization failed for '%s' (ffmpeg exit=%s); keeping original audio.",
                audio_path,
                result.returncode,
            )
            return False

        shutil.move(temp_path, audio_path)
        logger.info("Normalized narration loudness for '%s' to %.1f LUFS.", audio_path, target_lufs)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Loudness normalization raised for '%s': %s", audio_path, exc)
        return False
    finally:
        if os.path.isfile(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _derive_sentence_timings(
    narration: str, word_timings: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """
    Group word timings back into sentence-level timings using the
    original narration's sentence boundaries.

    Args:
        narration: Full narration text.
        word_timings: Word-level timing dicts, in order.

    Returns:
        A list of sentence timing dicts.
    """
    sentences = _split_sentences(narration)
    sentence_timings: List[Dict[str, Any]] = []
    cursor_index = 0

    for sentence in sentences:
        word_count = len(sentence.split())
        chunk = word_timings[cursor_index : cursor_index + word_count]
        if not chunk:
            continue
        sentence_timings.append(
            {
                "sentence": sentence,
                "start": chunk[0]["start"],
                "end": chunk[-1]["end"],
            }
        )
        cursor_index += word_count

    return sentence_timings


def run(input_json: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate narration audio and word/sentence timings for a script.

    Args:
        input_json: Must contain "run_id", "topic", and "script"
            (with a non-empty "narration").

    Returns:
        A standardized JSON response envelope (see module docstring).
    """
    try:
        require_keys(input_json, ["run_id", "topic", "script"])

        run_id = input_json["run_id"]
        topic = input_json["topic"]
        script = input_json["script"]

        if not isinstance(script, dict):
            raise ContractError("script must be a dict")

        narration = script.get("narration", "")
        if not narration.strip():
            raise ContractError("script.narration must be a non-empty string")

        seed = int(hashlib.sha256(run_id.encode("utf-8")).hexdigest(), 16)
        rng = random.Random(seed)
        voice = rng.choice(pipeline_config.VOICE_CANDIDATES)
        rate = rng.choice(pipeline_config.VOICE_SPEED_RANGE)
        pitch = rng.choice(pipeline_config.VOICE_PITCH_RANGE)

        output_path = _audio_output_path(run_id)

        source = "edge_tts"
        try:
            word_timings = _generate_with_edge_tts(narration, voice, rate, pitch, output_path)
            sentence_timings = _derive_sentence_timings(narration, word_timings)
            if pipeline_config.VOICE_LOUDNESS_NORMALIZATION_ENABLED:
                _normalize_loudness(output_path, pipeline_config.VOICE_TARGET_LUFS)
        except VoiceGenerationError as exc:
            logger.warning(
                "voice_generator falling back to simulated timings for run_id=%s: %s",
                run_id,
                exc,
            )
            word_timings, sentence_timings = _simulate_timings(
                narration, pipeline_config.VOICE_NATURAL_PAUSE_MS, rate
            )
            source = "simulated_fallback"

        logger.info(
            "Voice generated for run_id=%s topic='%s' source=%s voice=%s (%d words)",
            run_id,
            topic,
            source,
            voice,
            len(word_timings),
        )

        data = {
            "run_id": run_id,
            "topic": topic,
            "audio_path": output_path,
            "voice": voice,
            "speed": rate,
            "pitch": pitch,
            "word_timings": word_timings,
            "sentence_timings": sentence_timings,
            "source": source,
        }

        return build_response(module=MODULE_NAME, status="success", data=data)

    except ContractError as exc:
        logger.error("Voice Generator contract violation: %s", exc)
        return build_response(module=MODULE_NAME, status="error", error=str(exc))

    except Exception as exc:  # noqa: BLE001
        logger.exception("Voice Generator failed unexpectedly.")
        return build_response(module=MODULE_NAME, status="error", error=str(exc))
