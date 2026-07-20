---
description: Ask CommonTrace for a solution to the problem you're facing now — retrieval twin of /trace
argument-hint: "[keywords about the problem, e.g. gandi http]"
allowed-tools: ["Bash"]
---

Retrieval twin of `/trace`: force a CommonTrace search for a solution to the problem in THIS conversation. The endpoint is below — do NOT go rediscover it.

## 1 · Frame the query

- `$ARGUMENTS` present → the specific problem those keywords point at.
- else → the most recent unresolved problem in the conversation.

If `$ARGUMENTS` is an error message or stack trace, do NOT just search the raw text. Instead, build a canonical `error_signature` from the exception class and message. Signals: `Traceback`, `Error:`, `Exception:`, `sqlalchemy.exc.`, etc.

## 2 · Search

```
KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")

# Locate the skill hooks so we can reuse canonical_signature()
HOOKS="${CLAUDE_PLUGIN_ROOT:+$CLAUDE_PLUGIN_ROOT/hooks}"
[ -d "$HOOKS" ] || HOOKS="$(dirname "$(readlink -f ~/.claude/commands/recall.md)")/../hooks"

# Build the search payload safely in Python so arguments with quotes,
# spaces, or newlines cannot break the JSON or shell quoting.
PAYLOAD=$(
  CT_ARGS="$ARGUMENTS" PYTHONPATH="$HOOKS" python3 - <<'PY'
import json, os, sys
from session_state import canonical_signature

text = os.environ.get("CT_ARGS", "")
is_error = any(s in text.lower() for s in (
    'traceback', 'error:', 'exception:', '.exc.',
))
if is_error:
    sig = canonical_signature(text)
    if sig:
        payload = {"q": None, "error_signature": sig, "limit": 1}
    else:
        payload = {"q": text, "limit": 5}
else:
    payload = {"q": text, "limit": 5}
json.dump(payload, sys.stdout)
PY
)

curl -s -X POST https://api.commontrace.org/api/v1/traces/search \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d "$PAYLOAD"
```

Optionally add `"tags":["<language>"]` to the body to bias results.

## 3 · Present + recommend

From `results` (best first), show each as: **title** · 2-line context · 2-line solution · tags · id. Then state the one trace that actually fits the problem and the concrete next step to apply it. If nothing fits, say so plainly and keep solving.

If the API errors or is unreachable, say so and continue — do not retry or block.
