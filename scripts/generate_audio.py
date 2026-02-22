#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "elevenlabs>=1.0.0",
#   "pydub>=0.25.0",
#   "audioop-lts>=0.2.1",
# ]
# ///
"""
Generate interview audio from notes.md using ElevenLabs TTS.

Parses the 🎤 Interviewer / 👨‍💻 Candidate dialogue format and generates
a single MP3 with different voices for each speaker.

Usage:
    uv run scripts/generate_audio.py <path/to/notes.md>
    uv run scripts/generate_audio.py google-docs/notes.md
    uv run scripts/generate_audio.py google-docs/notes.md --output audio/google-docs.mp3

Environment variables:
    ELEVENLABS_API_KEY       required
    INTERVIEWER_VOICE_ID     optional (default: Rachel)
    CANDIDATE_VOICE_ID       optional (default: Adam)

Requirements:
    uv handles Python dependencies automatically via inline script metadata.
    Also requires ffmpeg: brew install ffmpeg  (or: sudo apt install ffmpeg)
"""

import argparse
import io
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Default ElevenLabs voice IDs — change these or override via env vars
# Browse voices at: https://elevenlabs.io/voice-library
# ---------------------------------------------------------------------------
DEFAULT_INTERVIEWER_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel — professional female
DEFAULT_CANDIDATE_VOICE   = "pNInz6obpgDQGcFmaJgB"  # Adam   — professional male


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
    # Curly quotes → straight (TTS handles these fine, but let's normalise)
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# ElevenLabs TTS
# ---------------------------------------------------------------------------

def tts(client, voice_id: str, text: str, model_id: str = "eleven_turbo_v2_5") -> bytes:
    """Call ElevenLabs TTS and return raw MP3 bytes."""
    audio_generator = client.text_to_speech.convert(
        voice_id=voice_id,
        text=text,
        model_id=model_id,
        output_format="mp3_44100_128",
    )
    return b"".join(audio_generator)


# ---------------------------------------------------------------------------
# Audio combining
# ---------------------------------------------------------------------------

def combine_clips(clips: list[bytes], pause_ms: int = 600) -> bytes:
    """Combine MP3 clips with a short silence between each."""
    from pydub import AudioSegment  # imported here so missing pydub gives a clear error

    silence = AudioSegment.silent(duration=pause_ms)
    combined = AudioSegment.empty()

    for clip_bytes in clips:
        segment = AudioSegment.from_mp3(io.BytesIO(clip_bytes))
        combined += segment + silence

    buf = io.BytesIO()
    combined.export(buf, format="mp3")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate interview audio from notes.md")
    parser.add_argument("notes", help="Path to notes.md file")
    parser.add_argument("--output", "-o", help="Output MP3 path (default: <notes_dir>/<dirname>.mp3)")
    parser.add_argument("--pause", type=int, default=600, help="Pause between speakers in ms (default: 600)")
    parser.add_argument("--model", default="eleven_turbo_v2_5", help="ElevenLabs model ID")
    parser.add_argument("--list-voices", action="store_true", help="Print available voices and exit")
    args = parser.parse_args()

    # --- API key ---
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("Error: ELEVENLABS_API_KEY environment variable not set.", file=sys.stderr)
        sys.exit(1)

    try:
        from elevenlabs.client import ElevenLabs
    except ImportError:
        print("Error: elevenlabs not installed. Run: uv run scripts/generate_audio.py", file=sys.stderr)
        sys.exit(1)

    client = ElevenLabs(api_key=api_key)

    # --- List voices mode ---
    if args.list_voices:
        voices = client.voices.get_all()
        print(f"{'Name':<30} {'Voice ID'}")
        print("-" * 60)
        for v in sorted(voices.voices, key=lambda x: x.name):
            print(f"{v.name:<30} {v.voice_id}")
        return

    # --- Voice IDs ---
    interviewer_voice = os.environ.get("INTERVIEWER_VOICE_ID", DEFAULT_INTERVIEWER_VOICE)
    candidate_voice   = os.environ.get("CANDIDATE_VOICE_ID",   DEFAULT_CANDIDATE_VOICE)

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
        output_path = notes_path.parent / f"{notes_path.parent.name}.mp3"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # --- Generate audio ---
    clips: list[bytes] = []
    for i, (speaker, text) in enumerate(segments, 1):
        voice_id = interviewer_voice if speaker == "interviewer" else candidate_voice
        label = "Interviewer" if speaker == "interviewer" else "Candidate"
        preview = text[:60] + ("…" if len(text) > 60 else "")
        print(f"[{i}/{len(segments)}] {label}: {preview}")

        try:
            audio_bytes = tts(client, voice_id, text, model_id=args.model)
            clips.append(audio_bytes)
        except Exception as e:
            print(f"  Warning: TTS failed for segment {i}: {e}", file=sys.stderr)
            continue

        # Small delay to stay within rate limits
        if i < len(segments):
            time.sleep(0.3)

    if not clips:
        print("Error: No audio clips generated.", file=sys.stderr)
        sys.exit(1)

    # --- Combine and save ---
    print(f"\nCombining {len(clips)} clips…")
    try:
        combined = combine_clips(clips, pause_ms=args.pause)
    except FileNotFoundError:
        print("Error: ffmpeg not found. Install it with: sudo pacman -S ffmpeg", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error combining audio: {e}", file=sys.stderr)
        sys.exit(1)

    output_path.write_bytes(combined)
    print(f"Saved: {output_path}")


if __name__ == "__main__":
    main()
