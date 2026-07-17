# Delibra

Delibra is a local research workspace for running Claude Code and Codex CLI as
first-class, human-directed collaborators. Each prompt streams into the browser,
persists as Markdown, and can be replied to or passed to another agent for critique.

## Setup

Requirements:

- Python 3.12+
- Claude Code 2.1.202, authenticated through its normal CLI login
- Codex CLI 0.144.5, authenticated through its normal CLI login

Create the project environment and install the reproducible lock:

```sh
python3.12 -m venv envs
envs/bin/pip install -r requirements.lock
```

Confirm both providers are available:

```sh
claude --version
codex --version
```

Run exactly one local worker, without reload:

```sh
envs/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`, register an existing absolute directory, create a
Claude or Codex agent from the project chat, and submit a prompt. The project chat
merges every agent's recorded rounds into one timeline; **Manage** opens project and
agent settings. The footer starts in a checking state, then reports missing CLIs and
version drift without delaying or preventing the rest of the UI from loading.

## Workflow and storage

Projects are registry entries pointing at existing directories. Unregistering never
deletes the user directory. Delibra owns only these locations:

```text
~/.delibra/registry.json
<project>/.delibra/manifest.json
<project>/.delibra/sessions/<id>/config.json
<project>/.delibra/sessions/<id>/rounds/round-NN.{prompt.md,partial.md,md}
<project>/.delibra/sessions/<id>/workspace/
```

Completed output files are the source of truth. A pass-to round stages a no-follow,
same-descriptor copy at `workspace/inputs/round-NN/source.md`, records its SHA-256,
and leaves the source round immutable. Session names never drive filesystem paths.

Native provider session IDs are used for replies. If a provider supplies no native
ID, Delibra stages at most 20 completed rounds and 2 MiB of history, newest-first for
selection and chronological for presentation. A newest round that cannot fit fails
clearly rather than being silently truncated.

Agent names, models, and effort levels remain editable after round 1; provider and
role instructions become fixed. Edits are rejected while an agent is running. Real
CLI gates verified that both Claude Code and Codex preserve native conversation state
when model and effort change, so the next round resumes natively with new settings.
The executable adapter capability flags remain the source of truth; an unproven or
failing provider falls back to bounded staged history with a visible round warning.

The chat sidebar can add or edit an agent without replacing the timeline or tearing
down another agent's live SSE connection. Completed output can be sent to a different
agent; session-namespaced fragment IDs keep simultaneous same-numbered rounds scoped
to the correct stream.

## Isolation and security boundary

Delibra provides write isolation, not read confidentiality:

- Agent cwd and writable scope are the session `workspace/`; private temp is
  `workspace/.tmp/`.
- Claude receives only Read/Write/Edit and native web tools under the verified safe
  command. Codex runs in `workspace-write`, with shell network disabled and native
  web search enabled.
- Codex provider state uses an app-owned `~/.delibra/codex-home`; subprocesses receive
  an allowlisted environment, not arbitrary server secrets.
- Agents may still read any path permitted to the operating-system user. Do not use
  Delibra as a confidentiality sandbox.
- The HTTP server is localhost-only, rejects non-loopback Host values and cross-site
  mutation Origins, and has no remote-user authentication. Do not bind it to LAN or
  public interfaces in the MVP.
- Markdown raw HTML is disabled and SSE text is HTML-escaped before HTMX swaps it.

All mutations use the lock order `registry -> project lifecycle -> session IDs in
sorted order`. A run never holds these locks while waiting for a model or SSE client.
This order also covers project unregister, session delete, and self/cross-session
pass-to operations.

## Recovery limits

Prompt and partial files are written before execution. Graceful cancellation,
timeouts, and shutdown terminate the entire CLI process group and finalize the round.
On startup, a persisted running round becomes an error round with any partial output
promoted and visible; the session remains runnable.

Final-output and metadata writes are best effort under storage failure. Delibra keeps
the partial until both are durable and emits/logs an error, but total storage loss
cannot guarantee an on-disk error record. A hard kill can orphan a provider process;
the MVP has no external watchdog. Graceful shutdown is the supported path.

## Configuration

The main environment settings are:

- `DELIBRA_HOME` (default `~/.delibra`)
- `DELIBRA_RUN_TIMEOUT` (default 900 seconds)
- `DELIBRA_OUTPUT_LIMIT` (default 10 MiB)
- `DELIBRA_REPLAY_LIMIT` (default 5 MiB)
- `DELIBRA_STATELESS_HISTORY_LIMIT` (default 2 MiB)
- `DELIBRA_STATELESS_ROUND_LIMIT` (default 20)
- `DELIBRA_REQUEST_BODY_LIMIT` (default 2 MiB)

Names/models are capped at 200 characters, role instructions at 20,000, prompts at
100,000, and pass instructions at 10,000.

## Verification

Run the suite with:

```sh
envs/bin/python -m pytest -q
```

The complete MVP acceptance record, including separate Claude and Codex parity
evidence, the M4 chat/config gate, and the documented browser-automation limitation,
is in
`docs/acceptance-mvp.md`. Sanitized CLI commands and behavioral isolation evidence
are in `spike/FINDINGS.md`.

The manually invoked real-provider gates are executable and disposable:

```sh
envs/bin/python -m spike.m4_config_resume_gate
envs/bin/python -m spike.m4_chat_gate
```
