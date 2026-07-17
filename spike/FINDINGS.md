# M0 CLI findings

<!-- CLAUDE-SPIKE:START -->
## Claude Code

- Executable: `/opt/homebrew/bin/claude`
- Version: `2.1.202 (Claude Code)`
- Model/effort exercised: `sonnet` / `low`; help advertises `low`, `medium`, `high`, `xhigh`, `max`.
- Streaming class: token-level `text_delta`; first delta at 17.477s before terminal at 19.260s; resumed delta at 14.204s before terminal at 15.491s.
- Resume strategy: **native**. `--resume <session-id>` recalled the seeded codeword and retained the same native session id.
- Stdin: both prompts were supplied on stdin.
- R4 boundary: **strict workspace-only writes** with private `TMPDIR=<workspace>/.tmp`. On first and resumed turns, the workspace write succeeded; attempted writes to adjacent project storage, another session, shared `/tmp`, and a pre-seeded symlink to outside were rejected. Exact writable root: `<workspace>`.
- Staged input: `workspace/inputs/source.md` was read on both turns (canary observed in the actual final text).
- Role instructions: required response prefix observed on both turns.
- Web: a `WebSearch` tool-use event was observed on both turns; Bash was absent from the allowed tool set.
- Ambient isolation: project and HOME `CLAUDE.md` sentinels plus project hook, skill, and MCP sentinels were behaviorally inert. `--safe-mode`, empty setting sources, strict empty MCP config, disabled slash commands, and no Chrome were active.
- Environment: authentication succeeded with only `PATH`, `HOME`, `USER`, `SHELL`, `LANG`, `LC_ALL`, `TERM`, and private `TMPDIR` when present.
- Prepared fallback: `spike/fixtures/claude_stateless_prompt.txt` records one bounded stateless-history prompt shape.
- Exit/timing: first rc=0, 19.791s; resume rc=0, 16.021s.

Proven first-turn command (prompt on stdin; placeholders are app values):

```text
claude -p --output-format stream-json --verbose --include-partial-messages --model <model> --effort <effort> --append-system-prompt <role> --permission-mode dontAsk --tools Read,Write,Edit,WebSearch,WebFetch --allowedTools 'Read(/**),Edit(/**),WebSearch,WebFetch' --safe-mode --setting-sources "" --strict-mcp-config --mcp-config '{"mcpServers":{}}' --disable-slash-commands --no-chrome
```

Resume appends `--resume <cli-session-id>` to the same command, retaining cwd and environment.
<!-- CLAUDE-SPIKE:END -->
