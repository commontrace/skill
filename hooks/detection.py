"""Spotting the moment "not knowing" turns into "knowing".

Four state transitions, detected from tool-use sequences alone — no LLM, no
NLU, no reading of the user's words. Each one appends a candidate to
candidates.jsonl for stop.py to score:

  research_then_implement — searched the web, then wrote code, no errors
  approach_reversal       — rewrote a file that had been Edit-ed 3+ times
  post_turn_revision         — same file edited before AND after a user turn
  test_fix_cycle          — tests failed, non-test code changed, tests pass

The fifth scored pattern, error_resolution, leaves no candidate: stop.py
derives it directly from the error / change / resolution event streams.

Every pattern here is scored in stop.py, and every pattern scored there is
detected here. A signal that fires but never counts is worse than no signal
at all — it looks like measurement while measuring nothing.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bash_result import detect_bash_error
from session_state import append_event, read_events

# Test commands for test_fix_cycle detection
TEST_COMMANDS = {
    "pytest", "jest", "mocha", "vitest", "cargo test", "go test",
    "npm test", "yarn test", "rspec", "phpunit", "unittest",
    "npm run test", "yarn run test",
}

# Documentation extensions — edits to these are prose, not a code correction.
_DOCS_EXTENSIONS = {".md", ".markdown", ".mdx", ".rst"}

_CODE_TOOLS = ("Write", "Edit", "NotebookEdit")


def _is_docs_only(file_path: str) -> bool:
    """True for documentation/markdown files.

    A markdown edit made after a user turn is almost always the agent writing
    up notes, not the user redirecting a wrong approach — surfacing it as a
    high-value ``post_turn_revision`` was noisy and misleading. Exclude docs.
    """
    return Path(file_path).suffix.lower() in _DOCS_EXTENSIONS


def _has_candidate(state_dir: Path, pattern: str,
                   extra_key: str = "") -> bool:
    """Check if a knowledge candidate of this type already exists."""
    for c in read_events(state_dir, "candidates.jsonl"):
        if c.get("pattern") == pattern:
            if extra_key and c.get("file") != extra_key:
                continue
            return True
    return False


def _tool_file_path(data: dict) -> str:
    ti = data.get("tool_input", {})
    return ti.get("file_path", "") if isinstance(ti, dict) else ""


def _tool_command(data: dict) -> str:
    ti = data.get("tool_input", {})
    return ti.get("command", "") if isinstance(ti, dict) else ""


def _detect_research_then_implement(data: dict, state_dir: Path,
                                    now: float) -> None:
    """Research events, then code, with no errors along the way."""
    research = read_events(state_dir, "research.jsonl")
    if not research or read_events(state_dir, "errors.jsonl"):
        return
    # Only fire if research was recent (within the last 10 minutes)
    if now - max(r.get("t", 0) for r in research) >= 600:
        return
    if _has_candidate(state_dir, "research_then_implement"):
        return
    changes = read_events(state_dir, "changes.jsonl")
    append_event(state_dir, "candidates.jsonl", {
        "pattern": "research_then_implement",
        "research_queries": [r.get("query", "")[:100] for r in research[-3:]],
        "file": _tool_file_path(data),
        "research_count": len(research),
        "changes_count": len(changes) + 1,
    })


def _detect_approach_reversal(data: dict, state_dir: Path) -> None:
    """A Write over a file that was Edit-ed 3+ times: the model was wrong."""
    file_path = _tool_file_path(data)
    if not file_path:
        return
    edit_count = sum(
        1 for c in read_events(state_dir, "changes.jsonl")
        if c.get("file") == file_path and c.get("tool") == "Edit"
    )
    if edit_count < 3 or _has_candidate(state_dir, "approach_reversal",
                                        file_path):
        return
    append_event(state_dir, "candidates.jsonl", {
        "pattern": "approach_reversal",
        "file": file_path,
        "previous_edits": edit_count,
    })


def _detect_post_turn_revision(data: dict, state_dir: Path, now: float) -> None:
    """Same file touched before AND after a user turn: the user redirected."""
    file_path = _tool_file_path(data)
    # Skip docs-only (*.md) edits — re-touching a markdown file across a
    # user turn is note-writing, not a redirected approach.
    if not file_path or _is_docs_only(file_path):
        return
    user_turns = read_events(state_dir, "user_turns.jsonl")
    changes = read_events(state_dir, "changes.jsonl")
    if not user_turns or len(changes) < 2:
        return
    last_turn_t = max(u.get("t", 0) for u in user_turns)
    pre_turn_edits = [
        c for c in changes
        if c.get("file") == file_path and c.get("t", 0) < last_turn_t
    ]
    # The current edit is AFTER the user turn (we are in post_tool_use).
    if not pre_turn_edits or now <= last_turn_t:
        return
    if _has_candidate(state_dir, "post_turn_revision", file_path):
        return
    append_event(state_dir, "candidates.jsonl", {
        "pattern": "post_turn_revision",
        "file": file_path,
        "pre_turn_edits": len(pre_turn_edits),
    })


def _detect_test_fix_cycle(data: dict, state_dir: Path) -> None:
    """Tests failed, non-test code changed, tests now pass."""
    command = _tool_command(data)
    is_error, _output, _error_text = detect_bash_error(data)
    if is_error or not any(tc in command for tc in TEST_COMMANDS):
        return
    test_failures = [
        e for e in read_events(state_dir, "errors.jsonl")
        if any(tc in e.get("command", "") for tc in TEST_COMMANDS)
    ]
    non_test_changes = [
        c for c in read_events(state_dir, "changes.jsonl")
        if "test" not in c.get("file", "").lower()
        and "spec" not in c.get("file", "").lower()
    ]
    if not test_failures or not non_test_changes:
        return
    if _has_candidate(state_dir, "test_fix_cycle"):
        return
    append_event(state_dir, "candidates.jsonl", {
        "pattern": "test_fix_cycle",
        "test_failures": len(test_failures),
        "fix_files": [c.get("file") for c in non_test_changes[:5]],
    })


def _detect_knowledge_candidates(tool_name: str, data: dict,
                                 state_dir: Path) -> None:
    """Detect knowledge crystallization moments from tool-use sequences.

    Writes candidates to candidates.jsonl when a state transition occurs.
    Each candidate captures the pattern type and surrounding context so
    the stop hook can score importance and pre-assemble contribution drafts.
    """
    now = time.time()

    if tool_name in _CODE_TOOLS:
        _detect_research_then_implement(data, state_dir, now)
        _detect_post_turn_revision(data, state_dir, now)
    if tool_name == "Write":
        _detect_approach_reversal(data, state_dir)
    if tool_name == "Bash":
        _detect_test_fix_cycle(data, state_dir)
