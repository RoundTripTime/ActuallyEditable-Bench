#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Bash): block background launches of heavy jobs without a memory cap.
A command that starts something in the background (nohup, or a trailing '&') running python / soffice / a shell
script must go through scripts/bg.sh or systemd-run --scope with MemoryMax. Exit 2 blocks the call (message to Claude)."""
import json
import re
import sys

try:
    cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
except Exception:
    sys.exit(0)
bg = re.search(r"\bnohup\b", cmd) or re.search(r"(?<!&)&(?!&)\s*(?:$|;|\n|echo|sleep|for|tail|cat)", cmd)  # '&&' is not background
heavy = re.search(r"python|soffice|\.sh\b|marker|docling|mineru|paddle", cmd)
capped = "scripts/bg.sh" in cmd or ("systemd-run" in cmd and "MemoryMax" in cmd)
if bg and heavy and not capped:
    print("BLOCKED (project rule after the WSL OOM freezes of 2026-10-14 / 2026-10-18): launch background jobs with "
          "scripts/bg.sh <name> <GB> <cmd...> (memory-capped systemd scope, 24 GB total budget).", file=sys.stderr)
    sys.exit(2)
sys.exit(0)
