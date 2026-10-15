"""Synthetic rollouts only: no real chats, network, model calls or configuration writes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/codex-compact-handoff/scripts/compact_handoff.py"
SPEC = importlib.util.spec_from_file_location("compact_handoff", SCRIPT)
monitor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = monitor
SPEC.loader.exec_module(monitor)


def token_record(tokens, **updates):
    result = {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg",
              "payload": {"type": "token_count", "info": {
                  "last_token_usage": {"input_tokens": tokens - 500, "output_tokens": 500,
                      "total_tokens": tokens, "cached_input_tokens": tokens - 1000,
                      "reasoning_output_tokens": 100},
                  "total_token_usage": {"total_tokens": 90_000_000},
                  "model_context_window": 258400}}}
    result["payload"]["info"].update(updates)
    return result


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.cwd = self.base / "project with spaces"
        self.cwd.mkdir()
        self.transcript = self.base / "rollout.jsonl"
        self.transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": "demo"}}) + "\n")
        self.state = self.base / "private-state"
        self.config = monitor.Config()
        self.env = {**os.environ, "CODEX_HANDOFF_DIR": str(self.state)}
        for key in list(self.env):
            if key.startswith("CODEX_HANDOFF_") and key != "CODEX_HANDOFF_DIR":
                del self.env[key]

    def append(self, record):
        with self.transcript.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def payload(self, event, **fields):
        return {"session_id": "demo", "cwd": str(self.cwd), "transcript_path": str(self.transcript),
                "hook_event_name": event, **fields}

    def hook(self, event, now=10000, **fields):
        return monitor.process_hook(self.payload(event, **fields), self.config, self.state, now=now)

    def cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin,
                              text=True, capture_output=True, env=self.env, check=False)

    def save(self, content="Goal: continue the diagnosis.\nNext: measure TTFT."):
        result = self.cli("save", "--transcript", str(self.transcript), "--cwd", str(self.cwd), stdin=content)
        self.assertEqual(result.returncode, 0, result.stderr)
        return Path(result.stdout.strip())

    def store(self):
        return monitor.Store(self.state, self.cwd, "demo")


class UsageTests(Sandbox):
    def test_latest_usage_not_cumulative_or_cached(self):
        self.append(token_record(120000))
        self.append(token_record(150000))
        usage = monitor.read_usage(self.transcript)
        self.assertEqual(usage.tokens, 150000)
        self.assertEqual(usage.window, 258400)

    def test_fallback_avoids_double_counting_reasoning(self):
        record = token_record(150000)
        del record["payload"]["info"]["last_token_usage"]["total_tokens"]
        self.append(record)
        self.assertEqual(monitor.read_usage(self.transcript).tokens, 150000)

    def test_cumulative_only_is_unknown_not_old_sample(self):
        self.append(token_record(150000))
        self.append(token_record(160000, last_token_usage=None))
        self.assertIsNone(monitor.read_usage(self.transcript).tokens)

    def test_null_rate_update_and_partial_line(self):
        self.append(token_record(150000))
        self.append({"type": "event_msg", "payload": {"type": "token_count", "info": None}})
        with self.transcript.open("a") as handle:
            handle.write('{"type":')
        self.assertEqual(monitor.read_usage(self.transcript).tokens, 150000)

    def test_no_metric_is_not_zero(self):
        self.assertIsNone(monitor.read_usage(self.transcript).tokens)

    def test_compaction_boundary_then_new_sample(self):
        self.append(token_record(200000))
        self.append({"type": "compacted", "payload": {}})
        self.assertIsNone(monitor.read_usage(self.transcript).tokens)
        self.append(token_record(20000))
        self.assertEqual(monitor.read_usage(self.transcript).tokens, 20000)

    def test_scanning_is_bounded(self):
        self.append(token_record(160000))
        with self.transcript.open("a") as handle:
            handle.write('x' * (monitor.MAX_SCAN + 100))
        self.assertIsNone(monitor.read_usage(self.transcript).tokens)

    def test_boolean_counter_is_not_a_token_total(self):
        self.append(token_record(160000, last_token_usage={"total_tokens": True}))
        self.assertIsNone(monitor.read_usage(self.transcript).tokens)


class LifecycleTests(Sandbox):
    def test_threshold_exactly_and_debounce(self):
        self.append(token_record(149999))
        self.assertEqual(self.hook("UserPromptSubmit"), {})
        self.append(token_record(150000))
        result = self.hook("UserPromptSubmit")
        self.assertIn("150,000", result["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("decision", result)
        self.assertEqual(self.hook("UserPromptSubmit", now=10001), {})
        self.append(token_record(180000))
        self.assertIn("systemMessage", self.hook("UserPromptSubmit", now=10002))

    def test_stop_continues_once_and_does_not_loop(self):
        self.append(token_record(150000))
        self.hook("UserPromptSubmit")
        self.assertEqual(self.hook("Stop")["decision"], "block")
        self.assertEqual(self.hook("Stop", now=10001), {})
        self.assertEqual(self.hook("Stop", now=11000, stop_hook_active=True), {})

    def test_stop_can_be_disabled(self):
        self.append(token_record(150000))
        self.config.stop_mode = "off"
        self.assertEqual(self.hook("Stop"), {})

    def test_snapshot_below_threshold_without_git_or_usage(self):
        with mock.patch.object(monitor, "git_facts", return_value={"available": False}):
            result = self.hook("PreCompact", trigger="auto")
        self.assertNotIn("continue", result)
        files = list(self.state.rglob("*-auto-*.md"))
        self.assertEqual(len(files), 1)
        self.assertIn("Observed context tokens: None", files[0].read_text())
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_snapshot_does_not_suppress_reasoned_handoff(self):
        self.append(token_record(150000))
        self.hook("PreCompact")
        self.assertEqual(self.hook("Stop")["decision"], "block")

    def test_saved_agent_note_suppresses_until_stale_or_growth(self):
        self.append(token_record(150000))
        saved = self.save()
        self.assertTrue(saved.is_file())
        now = monitor.time.time()
        self.assertEqual(self.hook("Stop", now=now), {})
        self.assertEqual(self.hook("Stop", now=now + 3700)["decision"], "block")
        self.append(token_record(200000))
        self.assertEqual(self.hook("UserPromptSubmit", now=now)["hookSpecificOutput"]["hookEventName"],
                         "UserPromptSubmit")

    def test_deleted_note_is_not_fresh(self):
        self.append(token_record(150000))
        self.save().unlink()
        self.assertEqual(self.hook("Stop")["decision"], "block")

    def test_compact_events_invalidate_old_metrics_and_expose_pointers(self):
        self.append(token_record(170000))
        self.hook("PreCompact")
        self.hook("PostCompact")
        result = self.hook("SessionStart", source="compact")
        self.assertIn("auto-", result["hookSpecificOutput"]["additionalContext"])
        result = self.hook("UserPromptSubmit")
        self.assertIn("unknown", result["systemMessage"])
        self.append(token_record(20000))
        self.assertEqual(self.hook("UserPromptSubmit"), {})
        self.append(token_record(170000))
        self.assertIn("systemMessage", self.hook("UserPromptSubmit"))

    def test_new_chat_and_worktree_do_not_share_index(self):
        self.append(token_record(150000))
        self.save()
        other = self.base / "other.jsonl"
        other.write_text(json.dumps({"type": "session_meta", "payload": {"id": "other"}}) + "\n")
        payload = self.payload("SessionStart", session_id="other", transcript_path=str(other))
        self.assertEqual(monitor.process_hook(payload, self.config, self.state), {})
        other_cwd = self.base / "worktree"
        other_cwd.mkdir()
        payload = self.payload("SessionStart", cwd=str(other_cwd))
        self.assertEqual(monitor.process_hook(payload, self.config, self.state), {})

    def test_mismatch_cannot_target_another_chat(self):
        result = self.cli("hook", stdin=json.dumps(self.payload("PreCompact", session_id="other")))
        self.assertEqual(result.returncode, 0)
        self.assertIn("unavailable", json.loads(result.stdout)["systemMessage"])
        self.assertFalse(list(self.state.rglob("*-auto-*.md")))


class FailureAndPrivacyTests(Sandbox):
    def test_bad_hook_payloads_fail_open_without_echoing_input(self):
        for data in ('{"password":"do-not-echo",', '[]', 'null'):
            result = self.cli("hook", stdin=data)
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("do-not-echo", result.stdout)
            self.assertNotIn('"continue": false', result.stdout)
            self.assertIsInstance(json.loads(result.stdout), dict)

    def test_missing_transcript_never_claims_saved(self):
        result = self.cli("hook", stdin=json.dumps(self.payload("PreCompact", transcript_path=None)))
        self.assertIn("unavailable", json.loads(result.stdout)["systemMessage"])
        self.assertNotIn("Saved", result.stdout)

    def test_snapshot_omits_transcript_secrets(self):
        self.append({"type": "response_item", "payload": {"text": "password=do-not-copy"}})
        with mock.patch.object(monitor, "git_facts", return_value={"available": False}):
            self.hook("PreCompact")
        note = next(self.state.rglob("*-auto-*.md")).read_text()
        self.assertNotIn("do-not-copy", note)

    def test_agent_note_redacts_common_credentials(self):
        secret = "sk-" + "a" * 30
        note = self.save("API_KEY=" + secret + "\nBearer abcdefghijklmnop\nNext: measure.").read_text()
        self.assertNotIn(secret, note)
        self.assertNotIn("abcdefghijklmnop", note)
        self.assertIn("Next: measure.", note)

    def test_symlink_state_root_is_refused(self):
        target = self.base / "elsewhere"
        target.mkdir(mode=0o700)
        self.state.symlink_to(target, target_is_directory=True)
        result = self.cli("hook", stdin=json.dumps(self.payload("PreCompact")))
        self.assertIn("unavailable", json.loads(result.stdout)["systemMessage"])
        self.assertEqual(list(target.iterdir()), [])

    def test_lock_contention_fails_open_and_preserves_state(self):
        store = self.store()
        try:
            with store.locked():
                result = self.cli("hook", stdin=json.dumps(self.payload("PreCompact")))
                self.assertEqual(result.returncode, 0)
                self.assertIn("unavailable", json.loads(result.stdout)["systemMessage"])
        finally:
            store.close()

    def test_oversized_notes_are_not_written(self):
        result = self.cli("save", "--transcript", str(self.transcript), "--cwd", str(self.cwd),
                          stdin="x" * (monitor.MAX_NOTE + 1))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(self.state.rglob("*-agent-*.md")))

    def test_invalid_configuration_fails_open_for_hooks(self):
        self.env["CODEX_HANDOFF_TOKEN_THRESHOLD"] = "not-an-int"
        result = self.cli("hook", stdin=json.dumps(self.payload("PreCompact")))
        self.assertEqual(result.returncode, 0)
        self.assertIn("Invalid configuration", json.loads(result.stdout)["systemMessage"])

    def test_git_checkpoint_does_not_copy_file_contents(self):
        try:
            subprocess.run(["git", "init", "-q", str(self.cwd)], check=True, capture_output=True)
        except FileNotFoundError:
            self.skipTest("Git unavailable")
        def git(*args):
            return subprocess.run(["git", "-C", str(self.cwd), *args], check=True, capture_output=True)
        (self.cwd / "tracked.txt").write_text("initial")
        git("add", "tracked.txt")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")
        (self.cwd / "tracked.txt").write_text("private contents not to copy")
        (self.cwd / ".env").write_text("password=do-not-copy")
        self.hook("PreCompact")
        note = next(self.state.rglob("*-auto-*.md")).read_text()
        self.assertIn("tracked.txt", note)
        self.assertNotIn("private contents", note)
        self.assertNotIn("do-not-copy", note)


class PackagingTests(unittest.TestCase):
    def test_shipped_hook_config_matches_generator(self):
        self.assertEqual(json.loads((ROOT / "hooks/hooks.json").read_text()), monitor.generate_hooks())

    def test_standalone_hook_command_handles_spaces(self):
        with tempfile.TemporaryDirectory(prefix="hook path ") as tmp:
            script = Path(tmp) / "compact helper.py"
            script.write_text(SCRIPT.read_text())
            result = subprocess.run([sys.executable, str(script), "hooks"], text=True, capture_output=True, check=True)
            config = json.loads(result.stdout)
            command = config["hooks"]["PreCompact"][0]["hooks"][0]["command"]
            result = subprocess.run(command, shell=True, input='{"hook_event_name":"Unrelated"}',
                                    text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout), {})


if __name__ == "__main__":
    unittest.main()
