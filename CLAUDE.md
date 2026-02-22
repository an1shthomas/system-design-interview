# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repository is a collection of system design interview questions and answers, formatted as mock interview dialogues (interviewer ↔ staff engineer candidate) for revision purposes.

## Folder Structure

Each question lives in its own folder:

```
<topic>/
  notes.md          ← formatted revision guide (interviewer/candidate dialogue format)
  diagram.excalidraw ← architecture diagram (open in Obsidian Excalidraw plugin or excalidraw.com)
  raw.txt           ← original transcript
```

Example: `metrics-monitoring/`

## Notes Format

`notes.md` files follow a consistent dialogue format:
- **🎤 Interviewer** asks the question
- **👨‍💻 Candidate** answers with reasoning and trade-offs
- Interviewer follow-up questions probe deeper
- Each step ends with a `✅ What makes this staff-level:` callout explaining what separates a strong answer from an average one
- Each step covers: Requirements & Scale → Core Entities → Architecture → API Design → Deep Dives → Trade-offs → Core Insight

When creating a new question, follow this format and structure.
