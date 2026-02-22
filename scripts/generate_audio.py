#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = [
#   "kokoro>=0.9.4",
#   "en-core-web-sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0.tar.gz",
#   "soundfile>=0.12.1",
#   "numpy>=1.24.0",
#   "elevenlabs>=1.0.0",
#   "pydub>=0.25.0",
# ]
# ///
"""
Generate interview audio from notes.md using Kokoro TTS (local) or ElevenLabs.

Parses the 🎤 Interviewer / 👨‍💻 Candidate dialogue format and generates
a single audio file with different voices for each speaker.

Usage:
    uv run scripts/generate_audio.py <notes.md>                          # Kokoro (default)
    uv run scripts/generate_audio.py <notes.md> --backend elevenlabs     # ElevenLabs

Kokoro (local, free, no API key):
    Requires: sudo pacman -S espeak-ng
    Output:   WAV file
    Voices:   INTERVIEWER_VOICE / CANDIDATE_VOICE (default: af_bella / am_michael)
    List:     --list-voices

ElevenLabs (cloud, paid):
    Requires: ELEVENLABS_API_KEY env var
    Output:   MP3 file
    Voices:   INTERVIEWER_VOICE / CANDIDATE_VOICE (default: Rachel / Adam voice IDs)
    List:     --list-voices
"""

import argparse
import os
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Voice defaults
# ---------------------------------------------------------------------------

KOKORO_INTERVIEWER_VOICE    = "af_jessica"             # Female, American English, grade A-
KOKORO_CANDIDATE_VOICE      = "am_michael"           # Male,   American English, grade B
KOKORO_SAMPLE_RATE          = 24000

ELEVENLABS_INTERVIEWER_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel
ELEVENLABS_CANDIDATE_VOICE   = "pNInz6obpgDQGcFmaJgB"  # Adam


# ---------------------------------------------------------------------------
# Markdown parsing (shared)
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

    def join_lines(lines: list[str]) -> str:
        """Join lines into a single string, adding a period between lines that
        don't already end with sentence-ending punctuation. This prevents list
        items from running together (e.g. '...ZooKeeper 2. It takes...')."""
        result = ""
        for line in lines:
            if not result:
                result = line
            elif result.endswith((".", "!", "?", ":", ",")):
                result += " " + line
            else:
                result += ". " + line
        return result

    def strip_list_marker(line: str) -> str:
        """Remove leading bullet or numbered list markers from a single line."""
        return re.sub(r"^(\d+[.)]\s+|[-*+]\s+)", "", line)

    def flush():
        nonlocal current_speaker, current_lines
        if current_speaker and current_lines:
            text = clean_text(join_lines(current_lines))
            if text.strip():
                segments.append((current_speaker, text))
        current_speaker = None
        current_lines = []

    for line in md_text.splitlines():
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if line.startswith(">"):
            continue
        if line.strip().startswith("|"):
            continue
        if re.match(r"^-{3,}$", line.strip()):
            continue
        if line.startswith("#"):
            flush()
            continue
        if "🎤" in line and "Interviewer" in line:
            flush()
            current_speaker = "interviewer"
            text = re.sub(r".*🎤\s*\*\*Interviewer[^:]*:\*\*\s*", "", line).strip()
            current_lines = [text] if text else []
            continue
        if ("👨" in line or "🧑" in line) and ("Candidate" in line or "Staff Engineer" in line):
            flush()
            current_speaker = "candidate"
            text = re.sub(r".*👨[^\s*]*\s*\*\*[^:]+:\*\*\s*", "", line).strip()
            current_lines = [text] if text else []
            continue
        if current_speaker:
            stripped = strip_list_marker(line.strip())
            if stripped:
                current_lines.append(stripped)

    flush()
    return segments


def clean_text(text: str) -> str:
    """Strip markdown formatting so text sounds natural when read aloud."""
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Kokoro backend
# ---------------------------------------------------------------------------

def kokoro_list_voices():
    print("American English:")
    print("  Female (af_*): af_heart(A) af_bella(A-) af_nicole(B-) af_aoede af_kore af_sarah af_alloy af_nova af_sky")
    print("  Male   (am_*): am_michael(B) am_fenrir(C+) am_puck(C+) am_echo am_eric am_liam am_onyx am_adam")
    print("British English:")
    print("  Female (bf_*): bf_emma(B-) bf_alice bf_isabella bf_lily")
    print("  Male   (bm_*): bm_george bm_fable bm_daniel bm_lewis")
    print("\nFull grades: https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md")


def kokoro_generate(notes_path: Path, output_path: Path, interviewer_voice: str,
                    candidate_voice: str, pause_ms: int):
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline

    md_text = notes_path.read_text(encoding="utf-8")
    segments = parse_dialogue(md_text)
    if not segments:
        print("No dialogue segments found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(segments)} dialogue segments.")
    print("Loading Kokoro model…")
    pipeline = KPipeline(lang_code='a')
    print(f"Interviewer: {interviewer_voice}  |  Candidate: {candidate_voice}\n")

    clips = []
    for i, (speaker, text) in enumerate(segments, 1):
        voice = interviewer_voice if speaker == "interviewer" else candidate_voice
        label = "Interviewer" if speaker == "interviewer" else "Candidate"
        print(f"[{i}/{len(segments)}] {label}: {text[:70]}{'…' if len(text) > 70 else ''}")
        try:
            chunks = [audio for _, _, audio in pipeline(text, voice=voice)]
            if chunks:
                clips.append(np.concatenate(chunks))
        except Exception as e:
            print(f"  Warning: TTS failed for segment {i}: {e}", file=sys.stderr)

    if not clips:
        print("Error: No audio clips generated.", file=sys.stderr)
        sys.exit(1)

    print(f"\nCombining {len(clips)} clips…")
    silence = np.zeros(int(KOKORO_SAMPLE_RATE * pause_ms / 1000), dtype=np.float32)
    combined = np.concatenate([x for clip in clips for x in (clip, silence)])

    sf.write(str(output_path), combined, KOKORO_SAMPLE_RATE)
    print(f"Saved: {output_path}  ({len(combined) / KOKORO_SAMPLE_RATE / 60:.1f} min)")


# ---------------------------------------------------------------------------
# ElevenLabs backend
# ---------------------------------------------------------------------------

def elevenlabs_list_voices(client):
    voices = client.voices.get_all()
    print(f"{'Name':<30} {'Voice ID'}")
    print("-" * 60)
    for v in sorted(voices.voices, key=lambda x: x.name):
        print(f"{v.name:<30} {v.voice_id}")


def elevenlabs_generate(notes_path: Path, output_path: Path, interviewer_voice: str,
                        candidate_voice: str, pause_ms: int, client, model_id: str):
    import io
    import time
    from pydub import AudioSegment

    md_text = notes_path.read_text(encoding="utf-8")
    segments = parse_dialogue(md_text)
    if not segments:
        print("No dialogue segments found.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(segments)} dialogue segments.")
    print(f"Interviewer: {interviewer_voice}  |  Candidate: {candidate_voice}\n")

    clips = []
    for i, (speaker, text) in enumerate(segments, 1):
        voice_id = interviewer_voice if speaker == "interviewer" else candidate_voice
        label = "Interviewer" if speaker == "interviewer" else "Candidate"
        print(f"[{i}/{len(segments)}] {label}: {text[:70]}{'…' if len(text) > 70 else ''}")
        try:
            audio_bytes = b"".join(client.text_to_speech.convert(
                voice_id=voice_id, text=text, model_id=model_id,
                output_format="mp3_44100_128",
            ))
            clips.append(audio_bytes)
        except Exception as e:
            print(f"  Warning: TTS failed for segment {i}: {e}", file=sys.stderr)
        if i < len(segments):
            time.sleep(0.3)

    if not clips:
        print("Error: No audio clips generated.", file=sys.stderr)
        sys.exit(1)

    print(f"\nCombining {len(clips)} clips…")
    silence = AudioSegment.silent(duration=pause_ms)
    combined = AudioSegment.empty()
    for clip_bytes in clips:
        combined += AudioSegment.from_mp3(io.BytesIO(clip_bytes)) + silence

    buf = io.BytesIO()
    combined.export(buf, format="mp3")
    output_path.write_bytes(buf.getvalue())
    print(f"Saved: {output_path}  ({combined.duration_seconds / 60:.1f} min)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate interview audio from notes.md")
    parser.add_argument("notes", help="Path to notes.md file")
    parser.add_argument("--backend", choices=["kokoro", "elevenlabs"], default="kokoro",
                        help="TTS backend (default: kokoro)")
    parser.add_argument("--output", "-o", help="Output file path (default: <notes_dir>/<dirname>.wav or .mp3)")
    parser.add_argument("--pause", type=int, default=600, help="Pause between speakers in ms (default: 600)")
    parser.add_argument("--model", default="eleven_turbo_v2_5", help="ElevenLabs model ID (ignored for kokoro)")
    parser.add_argument("--list-voices", action="store_true", help="Print available voices and exit")
    args = parser.parse_args()

    notes_path = Path(args.notes)

    interviewer_voice = os.environ.get("INTERVIEWER_VOICE")
    candidate_voice   = os.environ.get("CANDIDATE_VOICE")

    # --- Kokoro ---
    if args.backend == "kokoro":
        if args.list_voices:
            kokoro_list_voices()
            return

        iv = interviewer_voice or KOKORO_INTERVIEWER_VOICE
        cv = candidate_voice   or KOKORO_CANDIDATE_VOICE

        if args.output:
            output_path = Path(args.output)
        else:
            output_path = notes_path.parent / f"{notes_path.parent.name}-kokoro.wav"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        kokoro_generate(notes_path, output_path, iv, cv, args.pause)

    # --- ElevenLabs ---
    elif args.backend == "elevenlabs":
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            print("Error: ELEVENLABS_API_KEY environment variable not set.", file=sys.stderr)
            sys.exit(1)

        from elevenlabs.client import ElevenLabs
        client = ElevenLabs(api_key=api_key)

        if args.list_voices:
            elevenlabs_list_voices(client)
            return

        iv = interviewer_voice or ELEVENLABS_INTERVIEWER_VOICE
        cv = candidate_voice   or ELEVENLABS_CANDIDATE_VOICE

        if args.output:
            output_path = Path(args.output)
        else:
            output_path = notes_path.parent / f"{notes_path.parent.name}-elevenlabs.mp3"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        elevenlabs_generate(notes_path, output_path, iv, cv, args.pause, client, args.model)


if __name__ == "__main__":
    main()
