# CommonTrace Known-Fix Validation Runbook

Use this runbook after deploying the `error_signature` canonical lookup feature.

## Goal

Confirm that the skill can detect a known error, call `search_traces(error_signature=...)`, and return the canonical MissingGreenlet trace without embedding cost.

## Prerequisites

- API is running with migration `0024_error_signature` and `0025_successful_applications` applied.
- Seed fixture imported (`uv run python -m scripts.import_seeds` in `server/api/`).
- Skill hooks are installed in Claude Code.
- `~/.commontrace/config.json` has a valid `api_key`.

## Step 1: Verify seed fixture

Check that the canonical seed has an `error_signature`:

```bash
cd /path/to/server/api
uv run python - <<'PY'
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import settings
from app.models.trace import Trace

async def main():
    engine = create_async_engine(settings.database_url)
    session = async_sessionmaker(engine, expire_on_commit=False)()
    row = await session.execute(
        "SELECT title, error_signature FROM traces WHERE is_seed = true AND title ILIKE '%missinggreenlet%'"
    )
    print(row.fetchall())
    await session.close()
    await engine.dispose()
asyncio.run(main())
PY
```

Expected: a row with `error_signature = 'sqlalchemy.exc.missinggreenlet.greenlet.spawn.has.not.been.called'`.

## Step 2: Simulate a Bash error

Run a Python script that raises `sqlalchemy.exc.MissingGreenlet`:

```bash
python3 - <<'PY'
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.orm import sessionmaker

# Accessing sync ORM from async context without greenlet spawn
try:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = sessionmaker(bind=engine)
    session = Session()
    list(session.execute("SELECT 1"))
except Exception as e:
    print(e)
PY
```

The Claude Code `Bash` tool should trigger `post_tool_use.py` and emit the CommonTrace hook output.

## Step 3: Inspect the hook output

Look for a `PostToolUse` hook message with:

```
CommonTrace found relevant traces for this error:
1. [SQLAlchemy 2.0 async relationship loading strategies] — Use `selectin` or `joined` loading... (ID: <uuid>)
```

The result should be returned in one request, not after a long semantic search.

## Step 4: Verify the agent applied the fix

If the next Bash command succeeds, the hook should:

- Emit the `Resolved-with` trailer naming the CommonTrace trace.
- Call `POST /api/v1/traces/{id}/apply` to increment `successful_applications`.

Check the API server logs for:

```
{"successful_applications": N, "id": "<trace-uuid>"}
```

## Step 5: Measure metrics

Run the unit test suite for canonical signature extraction:

```bash
cd /path/to/skill
python3 -m pytest tests/test_canonical_signature.py -v
```

For an end-to-end metric snapshot, run:

```bash
cd /path/to/skill/hooks
python3 - <<'PY'
import sys, os
from session_state import canonical_signature
samples = [
    "sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called; can't workaround asyncio greenlet spawn",
    "Error: Cannot find module 'express'",
    "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/foo'",
]
for s in samples:
    print(canonical_signature(s))
PY
```

## Expected outcomes

| Check | Expected |
|---|---|
| Canonical search returns result | within 200 ms, `total == 1` |
| Result `error_signature` matches seed | exact match |
| `successful_applications` increments | `apply_trace` returns `N + 1` |
| Unit tests pass | `test_canonical_signature.py` green |

## If it fails

1. Confirm `error_signature` normalization: the seed and the hook must both call `canonical_signature()` (lowercase dotted).
2. Check API logs for `/api/v1/traces/search` requests containing `error_signature`.
3. Ensure the API migration `0025_successful_applications` is applied.
