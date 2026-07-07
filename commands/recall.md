---
description: Ask CommonTrace for a solution to the problem you're facing now — retrieval twin of /trace
argument-hint: "[keywords about the problem, e.g. gandi http]"
allowed-tools: ["Bash"]
---

Retrieval twin of `/trace`: force a CommonTrace search for a solution to the problem in THIS conversation. The endpoint is below — do NOT go rediscover it.

## 1 · Frame the query

- `$ARGUMENTS` present → the specific problem those keywords point at.
- else → the most recent unresolved problem in the conversation.

Build a focused query string — symptom + language/framework + key terms — not just the raw keywords.

## 2 · Search

```
KEY=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/.commontrace/config.json')))['api_key'])")
curl -s -X POST https://api.commontrace.org/api/v1/traces/search \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  --data-binary @- <<'JSON'
{"q":"<focused query>","limit":5}
JSON
```

Optionally add `"tags":["<language>"]` to the body to bias results.

## 3 · Present + recommend

From `results` (best first), show each as: **title** · 2-line context · 2-line solution · tags · id. Then state the one trace that actually fits the problem and the concrete next step to apply it. If nothing fits, say so plainly and keep solving.

If the API errors or is unreachable, say so and continue — do not retry or block.
