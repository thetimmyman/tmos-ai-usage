import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_usage
import usage_collector as uc


class ExtraAccountsTests(unittest.TestCase):
    def test_load_extra_accounts_validates_and_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            path.write_text(
                json.dumps(
                    {
                        "accounts": [
                            {
                                "id": "claude-code-2",
                                "name": "Claude Code (2)",
                                "type": "claude-code",
                                "config_dir": "~/.claude-account2",
                            },
                            {
                                "id": "antigravity-2",
                                "name": "Antigravity (2)",
                                "type": "antigravity",
                                "binary": "~/.local/bin/agy2",
                            },
                            # Rejects built-in collision
                            {
                                "id": "claude-code",
                                "name": "Dup Builtin",
                                "type": "claude-code",
                                "config_dir": "/tmp/x",
                            },
                            # Rejects duplicate extra id
                            {
                                "id": "claude-code-2",
                                "name": "Dup Extra",
                                "type": "claude-code",
                                "config_dir": "/tmp/y",
                            },
                            # Rejects invalid id characters
                            {
                                "id": "../bad",
                                "name": "Bad",
                                "type": "antigravity",
                                "binary": "/bin/true",
                            },
                            # Rejects unsupported type
                            {
                                "id": "codex-2",
                                "name": "Codex 2",
                                "type": "codex",
                                "config_dir": "/tmp/z",
                            },
                        ]
                    }
                )
            )
            loaded = uc.load_extra_accounts(path)
            self.assertEqual([a["id"] for a in loaded], ["claude-code-2", "antigravity-2"])
            self.assertEqual(loaded[0]["name"], "Claude Code (2)")
            self.assertEqual(loaded[1]["name"], "Antigravity (2)")

    def test_secondary_claude_uses_isolated_provider_and_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
            creds = root / ".credentials.json"
            creds.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "tok-secondary",
                            "subscriptionType": "max",
                            "expiresAt": int((now + timedelta(hours=2)).timestamp() * 1000),
                        }
                    }
                )
            )
            cfg = root / ".claude.json"
            cfg.write_text(json.dumps({"oauthAccount": {"accountUuid": "acct-2"}}))

            def fake_fetch(url, token, headers=None):
                return {
                    "five_hour": {
                        "utilization": 15.0,
                        "resets_at": "2026-11-23T17:00:00Z",
                    },
                    "seven_day": {
                        "utilization": 30.0,
                        "resets_at": "2026-11-30T00:00:00Z",
                    },
                }

            out = uc.collect_claude_code(
                now,
                state_dir=root,
                creds_path=creds,
                claude_config_path=cfg,
                fetch=fake_fetch,
                provider="claude-code-2",
                projects_root=root / "projects",
                label="Claude Code (2)",
            )
            self.assertEqual(out["provider"], "claude-code-2")
            self.assertEqual(out["label"], "Claude Code (2)")
            self.assertEqual(out["status"], "ok")
            self.assertTrue((root / "claude-code-2-usage-gate.json").is_file())
            self.assertFalse((root / "claude-usage-gate.json").is_file())

    def test_secondary_antigravity_uses_custom_provider_and_label(self):
        payload = {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "window": "5h",
                                    "remaining_fraction": 0.9,
                                    "reset_time": "2026-11-23T17:00:00Z",
                                }
                            ],
                        }
                    ]
                },
            },
        }
        with mock.patch.object(agy_usage, "cli_json", return_value=payload) as patched:
            out = agy_usage.collect(
                uc,
                provider="antigravity-2",
                binary="/tmp/fake-agy2",
                log_name="agy-antigravity-2.log",
                label="Antigravity (2)",
            )
        patched.assert_called_once()
        self.assertEqual(patched.call_args.kwargs.get("binary"), "/tmp/fake-agy2")
        self.assertEqual(out["provider"], "antigravity-2")
        self.assertEqual(out["label"], "Antigravity (2)")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["windows"][0]["remaining_pct"], 90.0)


if __name__ == "__main__":
    unittest.main()
