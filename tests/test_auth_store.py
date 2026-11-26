"""Accounts, invites and sessions, without HTTP."""
import json
import os
import subprocess
import sys
import threading
import unittest

from support import ROOT, qda_auth, temp_dir
from qda_store import StateDirError, Store, ensure_private_dir


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_auth(testcase, clock=None):
    state = temp_dir(testcase) / "state"
    state.mkdir(mode=0o700)
    store = Store(state)
    return qda_auth.Auth(store, clock=clock or Clock(), scrypt_n=2 ** 10), store


class Passwords(unittest.TestCase):
    def test_hash_round_trip(self):
        stored = qda_auth.hash_password("correct horse battery", n=2 ** 10)
        self.assertTrue(stored.startswith("scrypt$1024$8$1$"))
        self.assertTrue(qda_auth.verify_password("correct horse battery", stored))
        self.assertFalse(qda_auth.verify_password("correct horse batterY", stored))
        self.assertFalse(qda_auth.verify_password("x", "garbage"))
        self.assertFalse(qda_auth.verify_password("x", "scrypt$99999999$8$1$AA$AA"))

    def test_policy(self):
        self.assertIsNotNone(qda_auth.password_problem("short"))
        self.assertIsNone(qda_auth.password_problem("twelve chars"))
        self.assertIsNotNone(qda_auth.password_problem("x" * 1025))

    def test_emails_and_handles(self):
        self.assertEqual(qda_auth.normalize_email("  Alice@Example.EDU "), "alice@example.edu")
        self.assertIsNone(qda_auth.normalize_email("not-an-email"))
        self.assertIsNone(qda_auth.normalize_email("a@b"))
        self.assertEqual(qda_auth.default_handle("R.Garcia+lab@x.edu"), "r.garcia-lab")


class Invites(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.auth, self.store = make_auth(self, self.clock)

    def test_invite_accept_login(self):
        token = self.auth.create_invite("alice@example.edu", handle="alice")
        self.assertEqual(self.auth.invite_info(token)["email"], "alice@example.edu")
        email = self.auth.accept_invite(token, "a long passphrase")
        self.assertEqual(email, "alice@example.edu")
        self.assertIsNotNone(self.auth.authenticate("alice@example.edu", "a long passphrase"))
        self.assertIsNone(self.auth.authenticate("alice@example.edu", "wrong passphrase"))
        self.assertIsNone(self.auth.authenticate("nobody@example.edu", "a long passphrase"))
        # Used once, never again.
        self.assertIsNone(self.auth.invite_info(token))
        with self.assertRaises(qda_auth.AuthError):
            self.auth.accept_invite(token, "another passphrase")

    def test_secrets_never_on_disk(self):
        token = self.auth.create_invite("alice@example.edu")
        self.auth.accept_invite(token, "a long passphrase")
        session = self.auth.create_session("alice@example.edu")
        reset = self.auth.create_invite("alice@example.edu", kind="reset")
        blob = "".join(p.read_text() for p in self.store.dir.iterdir() if p.is_file())
        for secret in (token, session, reset, "a long passphrase"):
            self.assertNotIn(secret, blob)
        for p in self.store.dir.iterdir():
            self.assertEqual(os.stat(p).st_mode & 0o077, 0, p)

    def test_expiry_and_revocation(self):
        token = self.auth.create_invite("alice@example.edu")
        self.clock.t += qda_auth.INVITE_TTL + 1
        self.assertIsNone(self.auth.invite_info(token))
        token = self.auth.create_invite("alice@example.edu")
        newer = self.auth.create_invite("alice@example.edu")
        self.assertIsNone(self.auth.invite_info(token), "a new invite supersedes the old")
        invite_id = self.auth.overview()["invites"][0]["id"]
        self.auth.revoke_invite(invite_id)
        self.assertIsNone(self.auth.invite_info(newer))

    def test_rules(self):
        with self.assertRaises(qda_auth.AuthError):
            self.auth.create_invite("alice@example.edu", kind="reset")  # no account yet
        token = self.auth.create_invite("alice@example.edu", handle="al")
        with self.assertRaises(qda_auth.AuthError):
            self.auth.create_invite("alan@example.edu", handle="al")  # handle taken
        self.auth.accept_invite(token, "a long passphrase")
        with self.assertRaises(qda_auth.AuthError):
            self.auth.create_invite("alice@example.edu")  # already has an account
        with self.assertRaises(qda_auth.AuthError):
            self.auth.accept_invite(self.auth.create_invite("x@example.edu"), "too short")

    def test_link_used_exactly_once_under_race(self):
        token = self.auth.create_invite("alice@example.edu")
        results = []

        def accept():
            try:
                results.append(self.auth.accept_invite(token, "a long passphrase"))
            except qda_auth.AuthError:
                results.append(None)
            except qda_auth.Busy:
                results.append("busy")

        self.auth._hash_wait = 30
        threads = [threading.Thread(target=accept) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("alice@example.edu"), 1, results)

    def test_reset_replaces_password_and_ends_sessions(self):
        self.auth.accept_invite(self.auth.create_invite("alice@example.edu"), "first passphrase")
        session = self.auth.create_session("alice@example.edu")
        self.auth.accept_invite(self.auth.create_invite("alice@example.edu", kind="reset"),
                                "second passphrase")
        self.assertIsNone(self.auth.session_user(session))
        self.assertIsNone(self.auth.authenticate("alice@example.edu", "first passphrase"))
        self.assertIsNotNone(self.auth.authenticate("alice@example.edu", "second passphrase"))


class Sessions(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.auth, self.store = make_auth(self, self.clock)
        self.auth.accept_invite(self.auth.create_invite("alice@example.edu"), "a long passphrase")

    def test_idle_and_absolute_expiry(self):
        token = self.auth.create_session("alice@example.edu")
        self.assertEqual(self.auth.session_user(token)[0]["email"], "alice@example.edu")
        self.clock.t += qda_auth.SESSION_IDLE + 1
        self.assertIsNone(self.auth.session_user(token))
        token = self.auth.create_session("alice@example.edu")
        for _ in range(5):  # active every few days, but past the absolute limit
            self.clock.t += qda_auth.SESSION_ABSOLUTE / 5 + 1
            result = self.auth.session_user(token)
        self.assertIsNone(result)

    def test_disable_signs_out(self):
        token = self.auth.create_session("alice@example.edu")
        self.auth.set_disabled("alice@example.edu", True)
        self.assertIsNone(self.auth.session_user(token))
        self.assertIsNone(self.auth.authenticate("alice@example.edu", "a long passphrase"))

    def test_bad_tokens(self):
        for token in (None, "", "x" * 43 + "!", "short", "a" * 44):
            self.assertIsNone(self.auth.session_user(token))

    def test_session_cap(self):
        tokens = []
        for _ in range(12):
            self.clock.t += 1
            tokens.append(self.auth.create_session("alice@example.edu"))
        live = [t for t in tokens if self.auth.session_user(t)]
        self.assertEqual(live, tokens[-qda_auth.MAX_SESSIONS_PER_USER:])

    def test_other_process_changes_are_not_undone(self):
        """A CLI `disable` (another Store on the same directory) sticks, even
        while the server is refreshing that user's session."""
        token = self.auth.create_session("alice@example.edu")
        self.auth.session_user(token)
        cli = qda_auth.Auth(Store(self.store.dir), clock=self.clock, scrypt_n=2 ** 10)
        cli.revoke_user_sessions("alice@example.edu")
        self.clock.t += qda_auth.SEEN_GRANULARITY + 1  # server would now bump `seen`
        self.assertIsNone(self.auth.session_user(token))
        sessions = json.loads((self.store.dir / "sessions.json").read_text())["sessions"]
        self.assertEqual(sessions, {})


class StoreSafety(unittest.TestCase):
    def test_private_dir_checks(self):
        base = temp_dir(self)
        loose = base / "loose"
        loose.mkdir(mode=0o755)
        os.chmod(loose, 0o755)
        with self.assertRaises(StateDirError):
            ensure_private_dir(loose)
        ok = ensure_private_dir(base / "fresh")
        self.assertEqual(os.stat(ok).st_mode & 0o777, 0o700)
        (ok / "users.json").write_text("{}")
        os.chmod(ok / "users.json", 0o644)
        with self.assertRaises(StateDirError):
            ensure_private_dir(ok)

    def test_two_processes_do_not_lose_updates(self):
        state = temp_dir(self) / "state"
        state.mkdir(mode=0o700)
        script = (
            "import sys; sys.path.insert(0, sys.argv[1])\n"
            "from qda_store import Store\n"
            "s = Store(sys.argv[2])\n"
            "for i in range(50):\n"
            "    s.update('n.json', {'items': []}, lambda d: d['items'].append(sys.argv[3]))\n")
        procs = [subprocess.Popen([sys.executable, "-c", script, str(ROOT), str(state), tag])
                 for tag in ("a", "b")]
        for p in procs:
            self.assertEqual(p.wait(timeout=60), 0)
        items = json.loads((state / "n.json").read_text())["items"]
        self.assertEqual(len(items), 100)


if __name__ == "__main__":
    unittest.main()
