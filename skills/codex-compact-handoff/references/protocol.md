# Protocol and compatibility

## Supported rollout shape

This is a Codex adapter, not a Claude transcript parser:

```json
{"type":"session_meta","payload":{"id":"demo-session"}}
{"timestamp":"2026-01-01T00:00:00Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":149500,"cached_input_tokens":140000,"output_tokens":500,"reasoning_output_tokens":100,"total_tokens":150000},"total_token_usage":{"total_tokens":9000000},"model_context_window":258400}}}
```

The observed total is **150000**, not 9000000, 290000 or 150100. `last_token_usage` describes the latest recorded model call. It is a proxy for recently occupied context, not a tokenizer for the current prompt. Tool output appended afterwards, pending reasoning and new input may not yet be reflected. A cached-token counter is a subset of input; reasoning output is not independently added.

`info: null` rate-limit updates are ignored. Unsupported latest usage records become unknown instead of silently falling back to older counts. Malformed/partial JSONL lines are ignored. Parsing reads at most the last 8 MiB plus a boundary line; individual lines above 1 MiB are skipped. If the usage event is outside that range, status is unknown, not zero. Rollout JSONL is not a stable API; add a redacted synthetic fixture before adapting to another schema.

Native `compacted` and `event_msg/context_compacted` records are boundaries. After `PostCompact` or `SessionStart(source=compact)`, usage samples already on disk are invalidated until a new sample is written. This avoids repeatedly warning from pre-compaction counts. Both lifecycle events may occur for one compaction; the epoch value is an invalidation generation, not a count of compactions.

## Hook contract

The script reads one object from stdin: `hook_event_name`, `session_id`, `transcript_path`, `cwd`, and event-specific fields. A session id conflicting with `session_meta.id` is rejected. A missing id falls back to the transcript metadata, then the explicit transcript path's hash. It never scans for unrelated sessions.

| Event | Behavior |
| --- | --- |
| UserPromptSubmit | If over threshold without a fresh note, emits `systemMessage` plus `hookSpecificOutput.additionalContext`; does not block user input |
| Stop | At a reminder boundary emits `decision: block` with a reason requesting one agent handoff; this means one continuation, not rejecting the user. `stop_hook_active` prevents recursion |
| PreCompact | Always saves a mechanical checkpoint, no threshold gate; returns only a status message, never `continue: false` |
| PostCompact | Invalidates old token samples and reminder state |
| SessionStart | Gives pointers for this exact workspace/session; `source=compact` also invalidates old samples |

Stop continuation is configurable (`CODEX_HANDOFF_STOP_MODE=off`). Interrupted turns have no handler here; cancellation is respected. A disabled Stop hook or uncooperative model may leave no reasoned note, while PreCompact can still save metadata. Handler errors emit a generic diagnostic and proceed; they do not veto compaction. Unknown counters are reported explicitly.

## State and freshness

Default root: `${XDG_STATE_HOME:-$HOME/.local/state}/codex-compact-handoff`. Override with `CODEX_HANDOFF_DIR`.

Within that root, a hash of the resolved workspace path and a hash of the exact session id isolate worktrees and chats. Each session contains `state.json`, `.lock`, timestamped `*-auto-*.md` and `*-agent-*.md`. Files are 0600 and directories 0700. POSIX advisory locking serializes short state updates; a simultaneous invocation fails open instead of waiting indefinitely. Atomic replacement protects index updates. A state directory owned by another user, accessible to other users, or itself a symlink is refused. This is not protection against a malicious process running as the same OS user.

Only an agent note can suppress reminders, not an automatic snapshot. It must exist as a regular file, be from the current invalidation epoch, be less than `MAX_AGE` seconds old and have context growth below `MAX_DRIFT`. Unknown note token counts do not suppress reminders. Reminders independently track prompt-time and Stop-time notifications; re-reminders occur after sufficient growth, elapsed time, or an observed count decrease.

The tool never deletes old notes. Review and remove them manually when desired. Storing outside the repository reduces accidental publication; the notes can still contain private paths or prose. No original transcript bodies, tool output, patches, file contents, untracked filenames, remote URLs or Git configuration are copied into automatic snapshots. Tracked filenames and branch names may still be sensitive. Redaction of agent text is best effort, not a guarantee.

## Runtime requirements and limits

- Python 3.10+, Linux/macOS/POSIX (`fcntl` and descriptor-relative filesystem operations). Windows is not supported by this release.
- Git is optional; failures/timeouts produce an explicit unavailable marker in snapshots.
- Automatic behavior requires local lifecycle hooks supported **and trusted** in the actual Codex client. A skill or plugin appearing in the UI does not establish hook execution.
- A remote/cloud client whose hooks do not execute in the environment holding the transcript cannot use these local hooks. Use manual commands where the rollout is accessible.
- Unit/integration fixtures validate the hook protocol; they do not constitute an end-to-end supported-version certification. Run the smoke check in the README in a disposable Codex conversation.

Official references checked 2026-10-15:

- [Codex hooks](https://learn.chatgpt.com/docs/hooks): lifecycle events, trust, output schemas, and unstable transcript interface.
- [Plugin packaging](https://developers.openai.com/plugins/build/plugins): compatibility manifest, skills and plugin hooks.
- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference): `model_auto_compact_token_limit`.

Inspired by [claude-compact-handoff](https://github.com/tmastalirsch/claude-compact-handoff)'s separation of mechanical checkpoints and reasoned handoffs. This implementation and adapter are independently written; Claude hook payloads and token-accounting rules are not reused.
