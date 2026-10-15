# Codex Compact Handoff

**A local Codex skill + lifecycle hooks for context warnings and compaction checkpoints.**

[中文说明](README.zh-CN.md) · [Protocol and limitations](skills/codex-compact-handoff/references/protocol.md) · MIT licensed

At **150,000 observed context tokens**, remind Codex to write a handoff. Before compaction, save a mechanical checkpoint even if no handoff was written. After compaction, provide pointers back to the saved artifacts.

Inspired by [tmastalirsch/claude-compact-handoff](https://github.com/tmastalirsch/claude-compact-handoff), independently implemented for Codex rollout records and hook schemas. No model API calls, telemetry, runtime network dependencies, or third-party Python dependencies.

## What it does

| Point | Action |
| --- | --- |
| New user message | Read the exact session's latest recorded token usage; notify at the threshold if no fresh agent note exists |
| End of a turn | Ask for one bounded continuation to write an agent handoff; configurable off, guarded against loops |
| Before compaction | Write a metadata-only checkpoint: workspace, session, observed tokens, Git HEAD/branch, and bounded tracked-file status |
| After compaction / session resume | Provide paths to this session's saved notes; invalidate pre-compaction usage samples |

Two artifacts serve different purposes:

- **Agent handoff:** model-written intent, decisions, rejected approaches, evidence, constraints and next steps.
- **Mechanical checkpoint:** reproducible local metadata. It cannot reconstruct reasoning or unsaved work.

The skill does not automatically open a new chat, run `/compact`, set an auto-compaction policy, or install itself. Threshold checks happen at hook boundaries, not continuously while the model is streaming. No exact count is guessed when usage is missing.

## Requirements and verification status

- Python **3.10+** on Linux or macOS; POSIX only in v0.1.0.
- For automatic execution: a Codex client that supports and **trusts local lifecycle hooks**. Some cloud/orchestrated surfaces do not execute local command hooks.
- Git is optional. A project without Git still gets a checkpoint.
- The supported JSONL schema is demonstrated in the protocol reference. **Rollout logs are not a stable public interface.**

Local verification: all 29 automated tests passed on Python 3.14, and the skill format validator passed. The Codex executable present during development reports `0.162.0-alpha.17.2`. A local rollout sample matched the parser shape. **No live Codex lifecycle session has been used to certify automatic hooks in this release.** Perform the smoke check below before relying on it. The CI workflow describes a Python 3.10/3.12/3.13 Linux/macOS matrix; adding it does not mean those remote jobs have already run.

## Install the skill

Clone/download this repository. From its root, copy the self-contained skill into the skill directory used by your Codex client. For the common existing `~/.codex/skills` setup:

```bash
mkdir -p "$HOME/.codex/skills"
# Do not overwrite an existing installation. Review it first if this test fails.
test ! -e "$HOME/.codex/skills/codex-compact-handoff" && \
  cp -R skills/codex-compact-handoff "$HOME/.codex/skills/"
```

You can now invoke `$codex-compact-handoff` in a subsequent turn. **Skill installation alone does not turn on automatic monitoring.** If your client uses a different skill directory, copy there instead.

## Enable automatic hooks (optional)

Choose **one** installation route; do not enable both or every hook will fire twice.

### A. Standalone hooks

Print a configuration containing an absolute, shell-quoted path to the installed helper:

```bash
python3 "$HOME/.codex/skills/codex-compact-handoff/scripts/compact_handoff.py" hooks \
  > /tmp/codex-compact-handoff-hooks.json
```

Review the generated JSON and the script. Add/merge its event entries into your supported Codex hook source, such as `~/.codex/hooks.json` or `<project>/.codex/hooks.json`. Preserve all existing hooks; do not overwrite a hook file wholesale. The generator only prints configuration and does not edit any settings. If the installed path moves, regenerate the commands.

Restart/reload the client as required, then use its hook trust flow to explicitly trust the reviewed definitions. Policies can disable hooks; trust and execution support must be verified in the actual client.

### B. Plugin packaging

The repository contains `.codex-plugin/plugin.json`, `skills/` and `hooks/hooks.json`. A local-plugin installation in a compatible Codex client can load the bundled skill and hooks. Commands use Codex's `PLUGIN_ROOT` environment variable; they do **not** rely on Claude-only variables. Enable/review/trust the plugin's hooks through your client's plugin UI. This repository does not claim a published marketplace entry or provide a fictional `owner/repo` install command.

Official references: [hooks and trust](https://learn.chatgpt.com/docs/hooks), [plugin packaging](https://developers.openai.com/plugins/build/plugins).

## Configuration

Set these environment variables **in the environment running the hooks**, then reload the client as necessary:

| Variable | Default | Meaning |
| --- | ---: | --- |
| `CODEX_HANDOFF_TOKEN_THRESHOLD` | `150000` | Observed token count at which to suggest handoff |
| `CODEX_HANDOFF_MAX_AGE` | `3600` | Agent-note freshness in seconds |
| `CODEX_HANDOFF_MAX_DRIFT` | `50000` | Context growth that makes an agent note stale |
| `CODEX_HANDOFF_REMIND_GROWTH` | `30000` | Additional growth before a repeated reminder |
| `CODEX_HANDOFF_REMIND_SECONDS` | `900` | Elapsed time before a repeated reminder |
| `CODEX_HANDOFF_STOP_MODE` | `once` | `once`: request a handoff continuation at a reminder boundary; `off`: never continue a turn for this purpose |
| `CODEX_HANDOFF_DIR` | XDG state directory | Private note/state root; default `~/.local/state/codex-compact-handoff` |

Numeric settings must be positive integers. Reminders and Stop continuations have independent cooldowns. A fresh saved agent handoff suppresses both; automatic snapshots alone do not. No old notes are automatically deleted.

For example, to disable Stop continuations while keeping prompt reminders and pre-compaction checkpoints:

```bash
export CODEX_HANDOFF_STOP_MODE=off
```

The tool does **not** change Codex's automatic compaction settings. If you independently use `model_auto_compact_token_limit`, put the handoff reminder threshold below your auto-compaction threshold to leave room to write a note; a large tool response can still jump over the interval. See [Codex configuration](https://learn.chatgpt.com/docs/config-file/config-reference).

## Manual use

Use the **exact current transcript** supplied by a hook/client. Never choose the newest rollout globally; another chat may be newer.

```bash
python3 skills/codex-compact-handoff/scripts/compact_handoff.py status \
  --transcript /absolute/current/rollout.jsonl --cwd /absolute/workspace

python3 skills/codex-compact-handoff/scripts/compact_handoff.py snapshot \
  --transcript /absolute/current/rollout.jsonl --cwd /absolute/workspace

python3 skills/codex-compact-handoff/scripts/compact_handoff.py save \
  --transcript /absolute/current/rollout.jsonl --cwd /absolute/workspace <<'NOTE'
Goal: ...
Constraints and pending approvals: ...
Decisions and evidence: ...
Current artifacts and verification: ...
Running/background work: ...
Open questions: ...
Next concrete step: ...
NOTE
```

`save` prints the private handoff path and records freshness metadata. In a new chat, give Codex that exact path and ask it to read the note, verify current state, and continue from the next step. This is a reference to work already authorized, not permission to expand scope.

## Token accounting

Uses the latest `event_msg → token_count → info → last_token_usage.total_tokens`. If the total is unavailable, falls back to `input_tokens + output_tokens` from that same sample.

- Never uses cumulative `total_token_usage` as context length.
- Never adds cached input again or separately adds reasoning output.
- Calls the result **latest observed context**, not an exact live count of the next request.
- Clears stale observations after compaction and reports unavailable/unsupported counters as unknown.

## Verify locally

```bash
python3 -m unittest discover -s tests -v
```

Tests use synthetic sessions and temporary directories. They cover threshold/debounce, token accounting, incomplete JSONL, compaction resets, note freshness, cross-session/worktree isolation, privacy behavior, locks, symlink refusal, CLI behavior and generated hook configuration.

### Live smoke check (user-controlled)

In a disposable project/conversation:

1. Install and trust one hook route. Set `CODEX_HANDOFF_TOKEN_THRESHOLD=1000` for that client process only; leave auto-compaction at its normal threshold. Start a new conversation.
2. Send a request and another follow-up so recorded usage exists. Confirm a context warning appears and references the correct transcript/workspace. If no hook runs, stop and diagnose client support/trust rather than claiming monitoring works.
3. At the turn boundary confirm at most one continuation requests a handoff. Read the printed agent note and review sensitive data handling.
4. Invoke `/compact` yourself. Confirm a new `*-auto-*.md` checkpoint exists and the post-compaction continuation receives artifact pointers. Verify metrics stay unknown until a new usage event arrives, then reflect the smaller window.
5. Restore/unset the test threshold. Disable/remove only this project's hook entries or uninstall its plugin to roll back; leave other hook definitions and saved notes intact.

This test may use your normal model quota. No installer or test in this repository launches it automatically.

## Privacy and failure behavior

State is stored outside the repository, under workspace/session hashes with 0700 directories and 0600 files. Automatic checkpoints do not copy transcript bodies, command output, file contents, diffs, Git remotes or untracked filenames. Agent notes get best-effort credential redaction; **review before sharing**. Private paths, filenames or prose can still be sensitive. Do not commit local rollouts or real handoffs.

Hook errors do not prevent compaction, stop commands, or restart interrupted work. If a snapshot cannot be saved, the hook reports monitoring unavailable; it does not claim recovery succeeded. The tool is not a backup of your files and cannot guarantee lossless compaction.

## Publish this project

The repository includes an MIT license, skill, plugin compatibility manifest, hook definitions, tests and CI configuration. Before publishing, review the contents and run tests. Replace repository URLs in your own fork's installation examples when you add them. You can create a GitHub repository and push this directory; no GitHub repository or external publication is created by these files.

## Acknowledgments

The mechanical-checkpoint / reasoned-handoff design is inspired by [claude-compact-handoff](https://github.com/tmastalirsch/claude-compact-handoff). This project is independently written, uses a different rollout parser and configuration prefix, and is not affiliated with OpenAI or the upstream author.
