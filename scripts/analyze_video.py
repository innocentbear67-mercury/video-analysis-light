#!/usr/bin/env python3
"""
Analyze a YouTube video transcript with the active Hermes chat model.

Usage:
    python3 analyze_video.py <url_or_video_id> [--prompt "question"] [--language en,zh]

Pipeline:
    1. Fetch transcript (youtube-transcript-api v1.x) + title via YouTube oEmbed
    2. Chunk into <=40K-char pieces with ~2K overlap, split at segment boundaries
    3. Resolve the parent Hermes session's current model + provider from state.db
    4. Send each chunk through a one-shot Hermes call using that exact runtime
       requesting dense timestamped notes on the user's question
    5. If more than one chunk, one synthesis call merges the notes into the final
       analysis; otherwise the single chunk's notes ARE the analysis
    6. Print the final analysis to stdout

Exit codes: 0 = success, 1 = any failure (transcript, model resolution, API).
Uses Hermes-managed credentials for the resolved provider.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

CHUNK_CHARS = 40_000
OVERLAP_CHARS = 2_000
SINGLE_CALL_LIMIT = 60_000
REQUEST_TIMEOUT = 300

# Video longer than this (seconds) triggers a scope warning — the skill is
# tuned for videos under ~30 minutes.
SCOPE_WARN_SECONDS = 30 * 60

DEFAULT_PROMPT = (
    "Analyze this video and return FOUR sections, in this exact order. "
    "STYLE: concise, no-nonsense, no overkill, no jargon. Short tight sentences. "
    "Plain words a smart reader understands without a glossary. Every sentence must "
    "earn its place — cut anything that does not add signal.\n"
    "1. SUMMARY — the whole video in one compact list. Open with the single "
    "core idea in one sentence, then 4-8 bullets of the most important concepts "
    "or points that carry the argument. No filler, no background noise. Someone "
    "reading ONLY this section should understand the entire video within seconds "
    "without watching it.\n"
    "2. ANALYSIS (LIGHT) — break down the jargon and the core idea. Explain the "
    "key concepts in plain language, why they matter, and how they fit together "
    "into the video's central thesis. Assume a smart reader who is not "
    "necessarily an expert in this field.\n"
    "3. CAVEATS — every caveat, unverified claim, hidden assumption, or point "
    "that could invalidate the video's argument. What does it overstate, omit, "
    "or quietly assume? Always include this section, even if you end up saying "
    "the claims are mostly sound.\n"
    "4. RELATE TO ME — given this context about the viewer: {context}. Tell the "
    "viewer exactly what to LEARN (2-4 specific takeaways to act on or look into) "
    "and what to AVOID (traps, overstated ideas, or actions this video would "
    "wrongly encourage). Be specific and direct."
)


def extract_video_id(url_or_id: str) -> str:
    """Extract the 11-character video ID from various YouTube URL formats."""
    url_or_id = url_or_id.strip()
    patterns = [
        r'(?:v=|youtu\.be/|shorts/|embed/|live/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return url_or_id


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fetch_transcript(video_id: str, languages=None):
    """Return transcript segments as list of {'text', 'start', 'duration'} dicts."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        print(
            "Error: youtube-transcript-api not installed. Run: "
            "uv pip install youtube-transcript-api",
            file=sys.stderr,
        )
        sys.exit(1)

    api = YouTubeTranscriptApi()
    if languages:
        result = api.fetch(video_id, languages=languages)
    else:
        result = api.fetch(video_id)
    return [
        {"text": seg.text, "start": seg.start, "duration": seg.duration}
        for seg in result
    ]


def fetch_metadata(video_id: str) -> dict:
    """Best-effort title/channel via oEmbed. Never fatal."""
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return {"title": data.get("title", ""), "author": data.get("author_name", "")}
    except Exception:
        return {"title": "", "author": ""}


def chunk_segments(segments) -> list:
    """Split timestamped lines into <=CHUNK_CHARS chunks with OVERLAP_CHARS overlap."""
    lines = [f"{format_timestamp(seg['start'])} {seg['text']}" for seg in segments]
    if sum(len(line) for line in lines) <= SINGLE_CALL_LIMIT:
        return ["\n".join(lines)]

    chunks = []
    current = []
    current_len = 0
    for line in lines:
        if current and current_len + len(line) > CHUNK_CHARS:
            chunks.append("\n".join(current))
            # Overlap: walk back until we've collected ~OVERLAP_CHARS from the tail
            carry = []
            carry_len = 0
            for prev in reversed(current):
                carry.insert(0, prev)
                carry_len += len(prev) + 1
                if carry_len >= OVERLAP_CHARS:
                    break
            current = carry
            current_len = carry_len
        current.append(line)
        current_len += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _load_default_model_provider() -> tuple[str, str]:
    """Fallback to the profile config when no parent session is available."""
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    config_path = hermes_home / "config.yaml"
    try:
        import yaml

        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        model_cfg = config.get("model") or {}
        if isinstance(model_cfg, str):
            return model_cfg.strip(), ""
        return (
            str(model_cfg.get("default") or model_cfg.get("model") or "").strip(),
            str(model_cfg.get("provider") or "").strip(),
        )
    except Exception as exc:
        print(f"Warning: could not read {config_path}: {exc}", file=sys.stderr)
        return "", ""


def resolve_active_model_provider(
    model_override: str = "", provider_override: str = ""
) -> tuple[str, str]:
    """Resolve the active parent session model/provider, then config fallback."""
    model = (model_override or "").strip()
    provider = (provider_override or "").strip()
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    session_id = (os.environ.get("HERMES_SESSION_ID") or "").strip()

    if session_id and (not model or not provider):
        db_path = hermes_home / "state.db"
        try:
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT model, model_config FROM sessions WHERE id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            if row:
                stored_model, raw_config = row
                try:
                    session_cfg = json.loads(raw_config or "{}")
                except (TypeError, ValueError):
                    session_cfg = {}
                model = model or str(
                    stored_model or session_cfg.get("model") or ""
                ).strip()
                provider = provider or str(session_cfg.get("provider") or "").strip()
        except Exception as exc:
            print(
                f"Warning: could not resolve session model from {db_path}: {exc}",
                file=sys.stderr,
            )

    if not model or not provider:
        default_model, default_provider = _load_default_model_provider()
        model = model or default_model
        provider = provider or default_provider

    if not model or not provider:
        print(
            "Error: could not resolve the active Hermes model/provider. "
            "Run from a Hermes session or pass --model and --provider.",
            file=sys.stderr,
        )
        sys.exit(1)
    return model, provider


def call_active_model(user_content: str, model: str, provider: str) -> str:
    """Run one isolated Hermes turn using the selected provider adapter."""
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    tmp_dir = hermes_home / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="video-analysis-light-",
            dir=tmp_dir,
            delete=False,
        ) as handle:
            handle.write(user_content)
            prompt_path = Path(handle.name)

        command = [
            "hermes",
            "chat",
            "-Q",
            "--ignore-rules",
            "--provider",
            provider,
            "--model",
            model,
            "--reasoning",
            "high",
            "--toolsets",
            "",
            "--source",
            "tool",
            "--max-turns",
            "1",
            "--query-file",
            str(prompt_path),
        ]
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=REQUEST_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(
                f"Error: {provider}/{model} analysis timed out after "
                f"{REQUEST_TIMEOUT}s.",
                file=sys.stderr,
            )
            sys.exit(1)

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown Hermes error").strip()
            print(
                f"Error: Hermes model call failed for {provider}/{model}: "
                f"{detail[:1000]}",
                file=sys.stderr,
            )
            sys.exit(1)

        raw_content = (result.stdout or "").strip()
        content_lines = [
            line
            for line in raw_content.splitlines()
            if not line.startswith("Warning: Unknown toolsets:")
            and not line.startswith("session_id:")
        ]
        content = "\n".join(content_lines).strip()
        if not content:
            print(
                f"Error: {provider}/{model} returned empty content. Hermes stderr: "
                f"{(result.stderr or '').strip()[:500]}",
                file=sys.stderr,
            )
            sys.exit(1)
        return content
    finally:
        if prompt_path is not None:
            try:
                prompt_path.unlink(missing_ok=True)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(
        description="Analyze a YouTube transcript with the active Hermes chat model"
    )
    parser.add_argument("url", help="YouTube URL or video ID")
    parser.add_argument("--prompt", "-p", default=DEFAULT_PROMPT,
                        help="Analysis question/instruction (default: structured overview analysis)")
    parser.add_argument("--language", "-l", default=None,
                        help="Comma-separated language codes (e.g. en,zh). Default: auto")
    parser.add_argument("--context", "-c", default="",
                        help="Short blurb about the viewer (who they are, what they work on) "
                             "to power the 'Relevance to me' section. Default: empty")
    parser.add_argument("--model", default="",
                        help="Override the active Hermes session model")
    parser.add_argument("--provider", default="",
                        help="Override the active Hermes session provider")
    args = parser.parse_args()

    model, provider = resolve_active_model_provider(args.model, args.provider)
    print(f"Analysis runtime: {provider}/{model}", file=sys.stderr)

    video_id = extract_video_id(args.url)
    if not re.fullmatch(r"[a-zA-Z0-9_-]{11}", video_id):
        print(f"Error: could not extract a video ID from: {args.url}", file=sys.stderr)
        sys.exit(1)

    languages = [l.strip() for l in args.language.split(",")] if args.language else None

    # Interpolate viewer context into the prompt (no-op for custom prompts that
    # don't contain the placeholder).
    viewer_context = args.context or "no viewer context was provided"
    user_prompt = args.prompt.replace("{context}", viewer_context)

    try:
        segments = fetch_transcript(video_id, languages)
    except Exception as e:
        msg = str(e).lower()
        if "disabled" in msg:
            print("Error: transcripts are disabled for this video.", file=sys.stderr)
        elif "no transcript" in msg or "not available" in msg:
            print("Error: no transcript found. Try --language en,zh or another language.",
                  file=sys.stderr)
        else:
            print(f"Error fetching transcript: {e}", file=sys.stderr)
        sys.exit(1)

    meta = fetch_metadata(video_id)
    title = meta["title"] or video_id
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"Analyzing: {title}" + (f" ({meta['author']})" if meta["author"] else ""),
          file=sys.stderr)

    # Scope guard: estimate video length from the last caption segment. This
    # skill is tuned for videos under ~30 minutes.
    est_duration = max(seg["start"] + seg["duration"] for seg in segments)
    if est_duration > SCOPE_WARN_SECONDS:
        print(
            f"Warning: video is ~{int(est_duration // 60)}:{int(est_duration % 60):02d} long "
            f"(>30 min). This skill is tuned for videos under ~30 minutes; the summary "
            f"may be thin for a video this long. Consider video-analysis for deeper work.",
            file=sys.stderr,
        )

    chunks = chunk_segments(segments)
    print(f"Transcript: {len(chunks)} chunk(s), "
          f"{sum(len(c) for c in chunks):,} chars total", file=sys.stderr)

    chunk_notes = []
    for i, chunk in enumerate(chunks, 1):
        instruction = (
            f"You are analyzing segment {i} of {len(chunks)} of a YouTube video transcript.\n"
            f"User's request: {user_prompt}\n\n"
            f"Transcript segment:\n{chunk}\n\n"
            f"Produce dense notes: what this segment covers, the main points and topics "
            f"discussed, any names/tools/figures mentioned, and any claims that seem "
            f"questionable or overstated. Include timestamps only if the user's request "
            f"asks for them. Only what is in this segment. No padding."
        )
        chunk_notes.append(call_active_model(instruction, model, provider))
        print(f"Chunk {i}/{len(chunks)} analyzed", file=sys.stderr)

    if len(chunk_notes) == 1:
        print(chunk_notes[0])
        return

    synthesis = (
        f"You are a working analyst. Below are notes from {len(chunk_notes)} segments of the "
        f"YouTube video \"{title}\" ({watch_url}).\n"
        f"User's request: {user_prompt}\n\n"
        f"Segment notes:\n"
        + "\n\n---\n\n".join(f"[Segment {i}]\n{note}" for i, note in enumerate(chunk_notes, 1))
        + "\n\nMerge these into ONE final answer following the structure of the user's "
          "request. Reconcile overlaps, include timestamps only if requested, and always "
          "flag any questionable or unsupported claims. Be direct — no fluff."
    )
    print(call_active_model(synthesis, model, provider))


if __name__ == "__main__":
    main()
