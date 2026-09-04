---
name: video-analysis-light
description: "YouTube transcript analysis with the active chat model — concise, no-nonsense."
version: 0.4.0
author: Kundi Wang, Hermes Agent
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [video, youtube, transcript, analysis]
    related_skills: [youtube-content, video-analysis, routing]
---

# Video Analysis Light

Transcript-first YouTube analysis using the **same model and provider as the active Hermes chat**. The script reads the parent session's current runtime from `state.db`, including session-scoped `/model` switches, then sends transcript chunks through Hermes's provider adapter with reasoning effort high.

Examples:
- Chat on `openai-codex / gpt-5.6-sol` → analysis uses that ChatGPT OAuth runtime.
- Chat switched to `openrouter / z-ai/glm-5.3` → analysis uses GLM-5.3 through OpenRouter.

Any model available in the user's Hermes install works — nothing is hard-coded to a specific provider or model name. The transcript is the whole input. Visual-only charts, slides, and on-screen text remain invisible. **Videos without captions are not supported** (the script fails with a clear error rather than guessing).

## When to Use

- `/video-analysis-light` with a YouTube URL
- A fast, digest-first read of a video: a **summary you can absorb in seconds**, a plain-language jargon breakdown, caveats, and what it means for you
- Transcript-based summary, critique, claim extraction, structure, or takeaways
- The user wants analysis quality to track whichever model currently serves the chat
- **Best for videos under ~30 minutes.** The script warns on stderr when a video runs longer.

**Don't use for:**
- Videos over ~30 minutes that need real depth — the summary gets thin; use a heavier pipeline
- Videos without captions — there is no fallback; the script exits with an error
- Visual questions on a captioned video — the transcript never contains what is on screen
- A request that explicitly names a different analysis model; use the script's `--model` and `--provider` overrides

## Default Output

When run without `--prompt`, the analysis has four sections, in order. **Style: concise, no-nonsense, no overkill, no jargon** — short tight sentences, plain words, every sentence earns its place:

1. **Summary** — the whole video as one compact list. One-line core idea + 4–8 bullets of the most important concepts. Reading only this should let the user understand the video in seconds without watching it.
2. **Analysis (light)** — jargon broken down in plain language; the core idea and how the key concepts fit together. For a smart reader who isn't an expert in the field.
3. **Caveats** — every unverified claim, hidden assumption, and point that could invalidate the argument. What the video overstates, omits, or quietly assumes.
4. **Relate to me** — what to LEARN (2–4 specific takeaways to act on or look into) and what to AVOID (traps or actions the video would wrongly encourage), given the viewer's context.

## Prerequisites

- `youtube-transcript-api` in the Hermes venv
- A valid Hermes credential for the active chat provider

That's it — no extra API keys. The script uses whatever provider the chat session is already authenticated against.

## How to Run

```bash
set -a && source "${HERMES_HOME:-$HOME/.hermes}/.env" && set +a

"${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python3" \
  SKILL_DIR/scripts/analyze_video.py "https://youtube.com/watch?v=VIDEO_ID"
```

The script automatically resolves the current session runtime from `HERMES_SESSION_ID` and `${HERMES_HOME}/state.db`. If no session is available, it falls back to `model.default` and `model.provider` in the active profile config.

## Quick Reference

```bash
# Default four-section analysis (Summary / Analysis / Caveats / Relate to me).
# --context powers "Relate to me" — pass the viewer's profile from the active session.
"${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python3" \
  SKILL_DIR/scripts/analyze_video.py "URL" \
  --context "Viewer profile blurb here."

# Custom question
"${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python3" \
  SKILL_DIR/scripts/analyze_video.py "URL" \
  --prompt "What is the bull case, and what does it ignore?"

# Explicit override when running outside the parent chat
"${HERMES_HOME:-$HOME/.hermes}/hermes-agent/venv/bin/python3" \
  SKILL_DIR/scripts/analyze_video.py "URL" \
  --provider openrouter --model z-ai/glm-5.3
```

## Procedure

1. **Extract the YouTube URL.** Accept `watch?v=`, `youtu.be/`, `shorts/`, `embed/`, `live/`, or a raw 11-character ID.
2. **Build the viewer context (required).** The "Relate to me" section only works if the viewer's context is injected. Build a compact, factual 3–5 sentence blurb from the active session's user profile + memory (what they work on, their goals, what matters to them). Pass it through `--context`. Do not invent details; if profile data is thin, pass what exists rather than padding.
3. **Run the script.** Use `terminal` with `timeout=300`. Do not manually route to any specific model — the parent session runtime is the source of truth. If stderr warns the video is over ~30 min, say so and offer a deeper analysis path.
4. **Confirm the runtime line.** Stderr must report `Analysis runtime: provider/model`, matching the active chat's model and provider exactly.
5. **Deliver the analysis.** Keep the four-section structure intact. Lightly reformat the model output without replacing its substance.
6. **Verify time-sensitive claims separately.** The model's "Caveats" section is analysis, not fact-checking.

## How Runtime Selection Works

1. Read `HERMES_SESSION_ID` exported by the terminal tool.
2. Query the matching row in `${HERMES_HOME}/state.db`.
3. Read `sessions.model` and `sessions.model_config.provider`.
4. Invoke `hermes chat` once per transcript chunk with the exact `--model` and `--provider`, `--reasoning high`, no toolsets, and `--source tool`.
5. For long transcripts, invoke the same runtime again to synthesize the chunk notes.

Using `hermes chat` is deliberate: it reuses Hermes's credential pools and provider transports. This supports API-key providers and OAuth/subscription providers alike; a hard-coded HTTP endpoint would not.

## Pitfalls

- **Current session, not global default.** Do not infer the runtime from `config.yaml` when `HERMES_SESSION_ID` exists; `/model` may have changed only this chat.
- **No hard-coded analysis model.** Any provider/model available in Hermes works — the active chat runtime decides.
- **No caption fallback.** If the video has no transcript, the script fails. Do not pretend otherwise or substitute a vision model silently.
- **Nested calls create hidden tool-source sessions.** This is expected and keeps transcript chunks isolated from the main conversation history.
- **Long videos are chunked.** Transcripts over roughly 60,000 characters are split into approximately 40,000-character chunks with overlap, then synthesized by the same active model.
- **Autogenerated captions can corrupt names and figures.** Label important numbers as caption-derived until checked against another source.
- **Visual-only content on captioned videos is still invisible.** The transcript never contains what is shown on screen — say so rather than pretending it does.
- **Live-date anchoring still matters.** Put the current as-of date in `--context` for market or news videos and verify every time-sensitive claim on the web.

## Verification

After a run:
1. Exit code is 0 and output is non-empty.
2. Stderr's runtime line exactly matches the active session's model and provider.
3. The analysis references specific transcript content.
4. The answer addresses the user's actual question.
