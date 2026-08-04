"""Record what a hook process actually receives, then get out of the way.

Deliberately shaped like the CommonTrace SessionStart hook: same shell-form
`command` wrapper, same plugin layout. The only job is to answer one question
that unit tests cannot: does Claude Code really export a plugin's userConfig
options into the hook's environment as CLAUDE_PLUGIN_OPTION_<KEY>, including
the ones declared `sensitive`?
"""

import json
import os
import sys

OUT = os.environ.get("ENVPROBE_OUT", "/tmp/envprobe-result.json")

seen = {k: v for k, v in os.environ.items() if k.startswith("CLAUDE_PLUGIN_")}
payload = {
    "plugin_option_vars": {k: v for k, v in seen.items()
                           if k.startswith("CLAUDE_PLUGIN_OPTION_")},
    "other_claude_plugin_vars": {k: v for k, v in seen.items()
                                 if not k.startswith("CLAUDE_PLUGIN_OPTION_")},
    "cwd": os.getcwd(),
}
try:
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
except OSError as e:
    print(f"envprobe: could not write {OUT}: {e}", file=sys.stderr)

# Valid hook output so the session is not disturbed.
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "envprobe ran."}}))
