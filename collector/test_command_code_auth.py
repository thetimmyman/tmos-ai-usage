"""A dead Command Code credential must not hide a live one.

Command Code rotates credentials out from under a machine: a key that worked last week answers
`401 Your API key has expired` today, and every source the collector knows about can hold a dead
one while a lower-priority source holds the key the last sign-in wrote. These tests pin the two
halves of that behavior at the collector's own seam, `collect_command_code()`: which sources are
read, and that a rejected source is passed over instead of ending the read.

Every token here is synthetic and the endpoint is a stand-in — no test reaches the network.
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import usage_collector as c

STALE = "synthetic-stale-key-0000000000000000"
FRESH = "synthetic-fresh-key-0000000000000000"
CREDITS = {
    "windowLimits": {"fiveHour": {"used": 25, "cap": 100, "resetAt": 4102444800000}},
    "credits": {"monthlyCredits": 0, "purchasedCredits": 0, "freeCredits": 0},
}
SUBSCRIPTION = {"data": {"planId": "synthetic-plan", "status": "inactive"}}


class Endpoint:
    """Command Code's billing API, accepting the tokens it was given and rejecting the rest.

    `reject` is the status a dead credential gets (401 for an expired key, 403 for a revoked one)
    and `fail` stands for an endpoint that is simply broken, which is not a statement about a key.
    """

    def __init__(self, accepted=(), reject=401, fail=None):
        self.accepted = set(accepted)
        self.reject = reject
        self.fail = fail
        self.calls = []

    def __call__(self, url, token=None, **kwargs):
        path = url.split("?")[0]
        self.calls.append((path, token))
        if self.fail is not None:
            raise self.rejection(url, self.fail, "Server Error")
        if token not in self.accepted:
            raise self.rejection(url, self.reject, "Unauthorized")
        if path.endswith("/billing/credits"):
            return CREDITS
        if path.endswith("/billing/subscriptions"):
            return SUBSCRIPTION
        return {}

    @staticmethod
    def rejection(url, status, message):
        """An HTTPError that is already closed, so it raises without a ResourceWarning."""
        error = urllib.error.HTTPError(url, status, message, Message(), io.BytesIO(b""))
        error.close()
        return error

    def keys_used(self, suffix):
        return [token for path, token in self.calls if path.endswith(suffix)]


class CommandCodeCredentialTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        env = patch.dict(
            os.environ,
            {
                "HOME": str(self.home),
                "COMMAND_CODE_API_BASE": "https://commandcode.invalid",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        for name in ("COMMAND_CODE_API_KEY", "PI_CODING_AGENT_DIR"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)

    # ---- the credential sources, in the order the collector reads them
    def secrets_file(self, key):
        path = self.home / "secrets" / "command-code.env"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"COMMAND_CODE_API_KEY={key}\n")

    def cli_login(self, key):
        """What `cmd login` writes."""
        path = self.home / ".commandcode" / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"apiKey": key, "userId": "synthetic-user"}))

    def pi_login(self, key, agent=None):
        """What Pi's `/login` writes: the key stored as OAuth credentials."""
        path = (agent or self.home / ".pi" / "agent") / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "commandcode": {
                        "type": "oauth",
                        "access": key,
                        "refresh": key,
                        "expires": 4102444800000,
                    },
                }
            )
        )

    def collect(self, endpoint):
        with patch.object(c, "http_json", endpoint):
            return c.collect_command_code()

    # ---- a rejected credential is passed over
    def test_a_dead_key_in_the_secrets_file_does_not_hide_a_fresh_pi_login(self):
        self.secrets_file(STALE)
        self.pi_login(FRESH)
        endpoint = Endpoint(accepted=[FRESH])
        row = self.collect(endpoint)
        self.assertEqual(row["status"], "ok")
        self.assertEqual([w["name"] for w in row["windows"]], ["5h"])
        self.assertEqual(endpoint.keys_used("/billing/credits"), [STALE, FRESH])

    def test_a_forbidden_answer_is_also_read_as_a_dead_credential(self):
        self.secrets_file(STALE)
        self.pi_login(FRESH)
        endpoint = Endpoint(accepted=[FRESH], reject=403)
        row = self.collect(endpoint)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(endpoint.keys_used("/billing/credits"), [STALE, FRESH])

    def test_an_endpoint_failure_is_not_retried_with_another_credential(self):
        self.secrets_file(STALE)
        self.pi_login(FRESH)
        endpoint = Endpoint(fail=500)
        with self.assertRaises(urllib.error.HTTPError):
            self.collect(endpoint)
        self.assertEqual(
            len(endpoint.calls), 1, "a broken endpoint is not a statement about a key"
        )

    # ---- precedence is unchanged for credentials that work
    def test_the_first_source_that_is_accepted_still_wins(self):
        self.secrets_file(STALE)
        self.pi_login(FRESH)
        endpoint = Endpoint(accepted=[STALE, FRESH])
        row = self.collect(endpoint)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(endpoint.keys_used("/billing/credits"), [STALE])

    def test_the_environment_key_is_read_before_the_secrets_file(self):
        os.environ["COMMAND_CODE_API_KEY"] = FRESH
        self.secrets_file(STALE)
        endpoint = Endpoint(accepted=[FRESH])
        row = self.collect(endpoint)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(endpoint.keys_used("/billing/credits"), [FRESH])

    # ---- the two sign-in stores the collector also reads
    def test_the_command_code_cli_login_is_a_credential_source(self):
        self.cli_login(FRESH)
        endpoint = Endpoint(accepted=[FRESH])
        row = self.collect(endpoint)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(endpoint.keys_used("/billing/credits"), [FRESH])

    def test_the_pi_credential_follows_the_configured_agent_directory(self):
        agent = self.home / "worker"
        self.pi_login(FRESH, agent=agent)
        os.environ["PI_CODING_AGENT_DIR"] = str(agent)
        endpoint = Endpoint(accepted=[FRESH])
        row = self.collect(endpoint)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(endpoint.keys_used("/billing/credits"), [FRESH])

    # ---- nothing works: say so, name the sources, leak nothing
    def test_every_rejected_credential_asks_for_a_sign_in_and_names_the_sources(self):
        self.secrets_file(STALE)
        self.cli_login(STALE)
        self.pi_login(STALE)
        endpoint = Endpoint(accepted=[])
        row = self.collect(endpoint)
        self.assertEqual(row["status"], "unauthenticated")
        self.assertEqual(len(endpoint.keys_used("/billing/credits")), 3)
        self.assertIn("cmd login", row["note"])
        self.assertIn("~/secrets/command-code.env", row["note"])
        self.assertIn("~/.commandcode/auth.json", row["note"])
        self.assertIn("~/.pi/agent/auth.json", row["note"])
        self.assertNotIn(STALE, row["note"])
        self.assertLessEqual(
            len(row["note"]), 180, "the hint must survive the note length cap"
        )

    def test_no_credential_anywhere_asks_for_a_sign_in(self):
        endpoint = Endpoint()
        row = self.collect(endpoint)
        self.assertEqual(row["status"], "unauthenticated")
        self.assertIn("cmd login", row["note"])
        self.assertEqual(endpoint.calls, [])
