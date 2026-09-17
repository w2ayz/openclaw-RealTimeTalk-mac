#!/usr/bin/env python3
"""PreToolUse hook, fired only for Bash calls matching `git commit*`
(filtered by the "if" clause in .claude/settings.json).

Gives early, in-tool-call feedback by re-running .githooks/pre-commit
before Claude Code even attempts the commit -- avoids a failed Bash call
the model has to notice and retry. The durable, editor-agnostic gate is
`git config core.hooksPath .githooks` (see CLAUDE.md); this hook also
activates that setting on first use, so opening this repo in Claude Code
self-installs the git-level gate too, not just the Claude-Code-side one.

${CLAUDE_PROJECT_DIR} is Claude Code's documented, portable reference to
the project root regardless of the hook process's own cwd -- see
https://code.claude.com/docs/en/hooks.
"""
import json
import os
import subprocess
import sys

root = os.environ.get("CLAUDE_PROJECT_DIR", "")
hook_path = os.path.join(root, ".githooks", "pre-commit") if root else ""

if not hook_path or not os.path.isfile(hook_path):
    print("{}")
    sys.exit(0)

# Idempotent -- activates the durable git-level gate for this clone too.
subprocess.run(
    ["git", "-C", root, "config", "core.hooksPath", ".githooks"],
    capture_output=True,
)

proc = subprocess.run(
    ["bash", hook_path], cwd=root, capture_output=True, text=True, timeout=30
)

if proc.returncode != 0:
    reason = (proc.stdout + proc.stderr).strip()[-3000:]
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
else:
    print("{}")
