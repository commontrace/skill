# envprobe — proves how Claude Code delivers `userConfig` to hooks

Unit tests can prove that our hooks *read* `CLAUDE_PLUGIN_OPTION_API_KEY`
correctly. They cannot prove the other half of the contract: that Claude Code
actually *sets* it, for a `sensitive` option, in a shell-form hook process.
This is a throwaway plugin that answers that on whatever machine you run it.

It writes nothing persistent. `--plugin-dir` loads a plugin for one session,
and `--settings` supplies the config inline, so neither your `settings.json`
nor your credentials are touched.

```bash
cd "$(mktemp -d)"
ENVPROBE_OUT=./result.json claude -p "reply ok" \
  --plugin-dir /path/to/commontrace-skill/tools/envprobe \
  --settings '{"enabledPlugins":{"envprobe@inline":true},"pluginConfigs":{"envprobe@inline":{"options":{"probe_secret":"SENTINEL_SECRET","probe_plain":"SENTINEL_PLAIN"}}}}' \
  --permission-mode dontAsk
cat ./result.json
```

Expected:

```json
"plugin_option_vars": {
  "CLAUDE_PLUGIN_OPTION_PROBE_PLAIN": "SENTINEL_PLAIN",
  "CLAUDE_PLUGIN_OPTION_PROBE_SECRET": "SENTINEL_SECRET"
}
```

## The one thing that will waste your afternoon

The `pluginConfigs` key is the **full plugin id**, `name@marketplace` — not the
bare name. With `"envprobe"` instead of `"envprobe@inline"` the hook still runs
and `plugin_option_vars` comes back **empty**, with no warning anywhere. If you
are debugging a missing option, read `CLAUDE_PLUGIN_DATA` out of the hook's
environment first: it ends in the sanitised id (`.../data/envprobe-inline`),
which tells you the key you should have used.

For a normally installed plugin the id is `commontrace@commontrace`, and
`claude plugin install --config api_key=...` writes it for you, so this only
bites when supplying `pluginConfigs` by hand.
