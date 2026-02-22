#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "kokoro>=0.9.4",
#   "soundfile>=0.12.1",
#   "numpy>=1.24.0",
# ]
# ///
"""
Generate interview audio from notes.md using Kokoro TTS (local, no API key).

Parses the 🎤 Interviewer / 👨‍💻 Candidate dialogue format and generates
a single WAV file with different voices for each speaker.

Usage:
    uv run scripts/generate_audio.py <path/to/notes.md>
    uv run scripts/generate_audio.py google-docs/notes.md
    uv run scripts/generate_audio.py google-docs/notes.md --output audio/google-docs.wav

Requirements:
    uv handles Python dependencies automatically via inline script metadata.
    Also requires espeak-ng: sudo pacman -S espeak-ng

Voices (American English):
    Interviewer: af_bella  (female, grade A-)
    Candidate:   am_michael (male,   grade B)

    Override via env vars: INTERVIEWER_VOICE and CANDIDATE_VOICE
    List all voices: --list-voices
"""

import argparse
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Default Kokoro voice IDs
# Full list: https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md
# ---------------------------------------------------------------------------
DEFAULT_INTERVIEWER_VOICE = "af_bella"   # Female, American English, grade A-
DEFAULT_CANDIDATE_VOICE   = "am_michael" # Male,   American English, grade B

SAMPLE_RATE = 24000  # Kokoro outputs at 24kHz

ALL_VOICES = [
    # American English
    "af_heart", "af_bella", "af_nicole", "af_aoede", "af_kore", "af_sarah",
    "af_alloy", "af_nova", "af_sky", "af_jessica", "af_river",
    "am_michael", "am_fenrir", "am_puck", "am_echo", "am_eric",
    "am_liam", "am_onyx", "am_adam",
    # British English
    "bf_emma", "bf_alice", "bf_isabella", "bf_lily",
    "bm_george", "bm_fable", "bm_daniel", "bm_lewis",
]


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

def parse_dialogue(md_text: str) -> list[tuple[str, str]]:
    """
    Parse notes.md into a list of (speaker, text) tuples.

    Speakers: "interviewer" | "candidate"
    Skips: code blocks, tables, callout boxes (> ✅), section headers.
    """
    segments: list[tuple[str, str]] = []
    current_speaker: str | None = None
    current_lines: list[str] = []
    in_code_block = False

    def flush():
        nonlocal current_speaker, current_lines
        if current_speaker and current_lines:
            text = clean_text(" ".join(current_lines))
            if text.strip():
                segments.append((current_speaker, text))
        current_speaker = None
        current_lines = []

    for line in md_text.splitlines():
        # Track fenced code blocks — skip their contents
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # Skip callout boxes (✅ staff-level sections, blockquotes)
        if line.startswith(">"):
            continue

        # Skip tables
        if line.strip().startswith("|"):
            continue

        # Skip horizontal rules
        if re.match(r"^-{3,}$", line.strip()):
            continue

        # Section headers — reset speaker context
        if line.startswith("#"):
            flush()
            continue

        # --- Interviewer speaker marker ---
        if "🎤" in line and "Interviewer" in line:
            flush()
            current_speaker = "interviewer"
            text = re.sub(r".*🎤\s*\*\*Interviewer[^:]*:\*\*\s*", "", line).strip()
            current_lines = [text] if text else []
            continue

        # --- Candidate speaker marker ---
        # Handles both "👨‍💻 **Candidate:**" and "👨‍💻 **Staff Engineer (candidate):**"
        if ("👨" in line or "🧑" in line) and ("Candidate" in line or "Staff Engineer" in line):
            flush()
            current_speaker = "candidate"
            text = re.sub(r".*👨[^\s*]*\s*\*\*[^:]+:\*\*\s*", "", line).strip()
            current_lines = [text] if text else []
            continue

        # Accumulate lines for the current speaker
        if current_speaker:
            stripped = line.strip()
            if stripped:
                current_lines.append(stripped)

    flush()
    return segments


def clean_text(text: str) -> str:
    """Strip markdown formatting so text sounds natural when read aloud."""
    # Bold and italic
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    # Inline code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Markdown links
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    # Bullet/numbered list markers
    text = re.sub(r"^[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # Underscores used for emphasis
    text = re.sub(r"_(.+?)_", r"\1", text)
    # Curly quotes → straight
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Kokoro TTS
# ---------------------------------------------------------------------------

def tts(pipeline, voice: str, text: str):
    """Generate audio for text using Kokoro. Returns a numpy float32 array."""
    import numpy as np
    chunks = [audio for _, _, audio in pipeline(text, voice=voice)]
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def combine_clips(clips, pause_ms: int = 600):
    """Concatenate numpy audio arrays with silence between each clip."""
    import numpy as np
    silence = np.zeros(int(SAMPLE_RATE * pause_ms / 1000), dtype=np.float32)
    parts = []
    for clip in clips:
        parts.append(clip)
        parts.append(silence)
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate interview audio from notes.md using Kokoro TTS")
    parser.add_argument("notes", help="Path to notes.md file")
    parser.add_argument("--output", "-o", help="Output WAV path (default: <notes_dir>/<dirname>.wav)")
    parser.add_argument("--pause", type=int, default=600, help="Pause between speakers in ms (default: 600)")
    parser.add_argument("--list-voices", action="store_true", help="Print available voices and exit")
    args = parser.parse_args()

    if args.list_voices:
        print("Available Kokoro voices (American English):")
        print("  Female: af_heart af_bella af_nicole af_aoede af_kore af_sarah af_alloy af_nova af_sky")
        print("  Male:   am_michael am_fenrir am_puck am_echo am_eric am_liam am_onyx am_adam")
        print("\nBritish English:")
        print("  Female: bf_emma bf_alice bf_isabella bf_lily")
        print("  Male:   bm_george bm_fable bm_daniel bm_lewis")
        print("\nFull quality grades: https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md")
        return

    # --- Voice selection ---
    interviewer_voice = os.environ.get("INTERVIEWER_VOICE", DEFAULT_INTERVIEWER_VOICE)
    candidate_voice   = os.environ.get("CANDIDATE_VOICE",   DEFAULT_CANDIDATE_VOICE)

    # --- Parse notes.md ---
    notes_path = Path(args.notes)
    if not notes_path.exists():
        print(f"Error: {notes_path} not found.", file=sys.stderr)
        sys.exit(1)

    md_text = notes_path.read_text(encoding="utf-8")
    segments = parse_dialogue(md_text)

    if not segments:
        print("No dialogue segments found. Check the notes.md format.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(segments)} dialogue segments.")

    # --- Output path ---
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = notes_path.parent / f"{notes_path.parent.name}.wav"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # --- Load Kokoro (downloads model on first run, ~300MB) ---
    print("Loading Kokoro model…")
    try:
        from kokoro import KPipeline
    except ImportError:
        print("Error: kokoro not installed. Run: uv run scripts/generate_audio.py", file=sys.stderr)
        sys.exit(1)

    pipeline = KPipeline(lang_code='a')  # 'a' = American English
    print(f"Interviewer voice: {interviewer_voice}  |  Candidate voice: {candidate_voice}\n")

    # --- Generate audio ---
    import numpy as np
    clips = []
    for i, (speaker, text) in enumerate(segments, 1):
        voice = interviewer_voice if speaker == "interviewer" else candidate_voice
        label = "Interviewer" if speaker == "interviewer" else "Candidate"
        preview = text[:70] + ("…" if len(text) > 70 else "")
        print(f"[{i}/{len(segments)}] {label}: {preview}")

        try:
            audio = tts(pipeline, voice, text)
            clips.append(audio)
        except Exception as e:
            print(f"  Warning: TTS failed for segment {i}: {e}", file=sys.stderr)

    if not clips:
        print("Error: No audio clips generated.", file=sys.stderr)
        sys.exit(1)

    # --- Combine and save ---
    print(f"\nCombining {len(clips)} clips…")
    combined = combine_clips(clips, pause_ms=args.pause)

    import soundfile as sf
    sf.write(str(output_path), combined, SAMPLE_RATE)
    duration_min = len(combined) / SAMPLE_RATE / 60
    print(f"Saved: {output_path}  ({duration_min:.1f} min)")


if __name__ == "__main__":
    main()
