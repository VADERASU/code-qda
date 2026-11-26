"""qda_auth — invite-only accounts, passwords and sessions for hosted mode.

Nobody can sign up. The admin creates an invite for an email address
(`app.py admin invite …` or the admin page), which yields a one-time link the
admin sends themselves; following it lets that person choose a password.
Password resets are the same kind of link. No HTTP in here — qda_hosted
turns requests into these calls.

Secrets on disk: passwords only as scrypt hashes, and invite and session
tokens only as SHA-256 digests, so a copy of the state directory cannot be
used to sign in. Stdlib only; hashlib.scrypt needs an OpenSSL-backed Python.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import OrderedDict, deque

USERS = "users.json"
INVITES = "invites.json"
SESSIONS = "sessions.json"
AUDIT = "audit.log"
EMPTY_USERS = {"version": 1, "users": {}}
EMPTY_INVITES = {"version": 1, "invites": {}}
EMPTY_SESSIONS = {"version": 1, "sessions": {}}

PASSWORD_MIN = 12
PASSWORD_MAX = 1024
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+")
HANDLE_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,30}[a-z0-9])?")

INVITE_TTL = 7 * 86400
RESET_TTL = 86400
SESSION_ABSOLUTE = 30 * 86400
SESSION_IDLE = 7 * 86400
SEEN_GRANULARITY = 3600
MAX_SESSIONS_PER_USER = 10


class AuthError(ValueError):
    """A user-facing refusal; the message is safe to show."""


class Busy(RuntimeError):
    """Too many password hashes in flight; ask the client to retry."""


# --- primitives -------------------------------------------------------------


def normalize_email(value):
    if not isinstance(value, str):
        return None
    email = unicodedata.normalize("NFKC", value).strip().lower()
    return email if len(email) <= 254 and EMAIL_RE.fullmatch(email) else None


def default_handle(email):
    local = re.sub(r"[^a-z0-9._-]+", "-", email.split("@", 1)[0].lower())
    local = local.strip("._-")[:32].strip("._-")
    return local or "user"


def password_problem(password):
    if not isinstance(password, str):
        return "Choose a password."
    n = len(unicodedata.normalize("NFKC", password))
    if n < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters."
    if n > PASSWORD_MAX:
        return f"Use at most {PASSWORD_MAX} characters."
    return None


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password, n=SCRYPT_N):
    salt = os.urandom(16)
    dk = hashlib.scrypt(unicodedata.normalize("NFKC", password).encode(),
                        salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
                        maxmem=64 * 1024 * 1024)
    return f"scrypt${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt, dk = stored.split("$")
        n, r, p = int(n), int(r), int(p)
        if scheme != "scrypt" or not (2 ** 10 <= n <= 2 ** 20 and r <= 16 and p <= 4):
            return False
        expected = _unb64(dk)
        got = hashlib.scrypt(unicodedata.normalize("NFKC", password).encode(),
                             salt=_unb64(salt), n=n, r=r, p=p,
                             dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError, AttributeError):
        return False
    return hmac.compare_digest(got, expected)


def new_token():
    """-> (token for the user, digest for the disk)."""
    token = secrets.token_urlsafe(32)
    return token, token_digest(token)


def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def valid_token(token):
    return isinstance(token, str) and TOKEN_RE.fullmatch(token) is not None


class RateLimiter:
    """Failures per key in a sliding window. Memory is bounded."""

    def __init__(self, limit, window, clock=time.monotonic, max_keys=10000):
        self.limit, self.window, self.clock = limit, window, clock
        self.max_keys = max_keys
        self._hits = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, key, now):
        hits = self._hits.get(key)
        if hits is None:
            return None
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if not hits:
            del self._hits[key]
            return None
        return hits

    def blocked(self, key):
        with self._lock:
            hits = self._prune(key, self.clock())
            return hits is not None and len(hits) >= self.limit

    def fail(self, key):
        with self._lock:
            now = self.clock()
            hits = self._prune(key, now)
            if hits is None:
                hits = self._hits[key] = deque()
            hits.append(now)
            self._hits.move_to_end(key)
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)

    def clear(self, key):
        with self._lock:
            self._hits.pop(key, None)


# --- accounts ---------------------------------------------------------------


class Auth:
    def __init__(self, store, clock=time.time, scrypt_n=SCRYPT_N,
                 hash_slots=2, hash_wait=2.0):
        self.store = store
        self.clock = clock
        self.scrypt_n = scrypt_n
        # Each scrypt hash holds 16 MiB and a core for ~50 ms. Cap how many run
        # at once so a burst of logins cannot exhaust memory or starve editing.
        self._slots = threading.BoundedSemaphore(hash_slots)
        self._hash_wait = hash_wait
        self._dummy = hash_password(secrets.token_urlsafe(16), n=scrypt_n)
        minute = 60
        # Per account: slows guessing at one password to 480 tries a day.
        # Per address (behind tailscaled only): one source can't sweep accounts.
        # Overall: a ceiling on hashing work. Set high, because reaching it
        # locks *everyone* out of signing in for a while (existing sessions
        # keep working), and that shouldn't take one attacker an afternoon.
        self.limit_email = RateLimiter(5, 15 * minute)
        self.limit_ip = RateLimiter(20, 15 * minute)
        self.limit_global = RateLimiter(500, 15 * minute)
        self.limit_tokens = RateLimiter(30, 15 * minute)

    def _hashing(self, fn, *args):
        if not self._slots.acquire(timeout=self._hash_wait):
            raise Busy()
        try:
            return fn(*args)
        finally:
            self._slots.release()

    def audit(self, event, **fields):
        record = {"ts": round(self.clock(), 3), "event": event}
        record.update({k: v for k, v in fields.items() if v is not None})
        self.store.append_jsonl(AUDIT, record)

    # --- users ----------------------------------------------------------

    def users(self):
        return self.store.read(USERS, EMPTY_USERS)["users"]

    def get_user(self, email):
        user = self.users().get(email)
        return dict(user, email=email) if user else None

    def has_users(self):
        return bool(self.users())

    def authenticate(self, email, password):
        """-> user dict, or None. Takes the same time whether or not the
        account exists, is disabled, or has no password yet."""
        email = normalize_email(email)
        user = self.get_user(email) if email else None
        stored = user.get("password") if user and not user.get("disabled") else None
        if not isinstance(password, str) or len(password) > PASSWORD_MAX * 4:
            password = ""
        ok = self._hashing(verify_password, password, stored or self._dummy)
        return user if ok and stored else None

    def set_disabled(self, email, disabled, by=None):
        def change(doc):
            user = doc["users"].get(email)
            if user is None:
                raise AuthError(f"no account for {email}")
            user["disabled"] = bool(disabled)
        self.store.update(USERS, EMPTY_USERS, change)
        if disabled:
            self.revoke_user_sessions(email)
        self.audit("user_disabled" if disabled else "user_enabled", email=email, by=by)

    # --- sessions -------------------------------------------------------

    def create_session(self, email):
        token, digest = new_token()
        now = self.clock()

        def change(doc):
            sessions = doc["sessions"]
            self._prune_sessions(sessions, now)
            mine = sorted((s["created"], d) for d, s in sessions.items()
                          if s.get("email") == email)
            for _, d in mine[:max(0, len(mine) - MAX_SESSIONS_PER_USER + 1)]:
                del sessions[d]
            sessions[digest] = {"email": email, "created": now, "seen": now}
        self.store.update(SESSIONS, EMPTY_SESSIONS, change)
        return token

    @staticmethod
    def _expired(session, now):
        return (now - session.get("created", 0) > SESSION_ABSOLUTE
                or now - session.get("seen", 0) > SESSION_IDLE)

    def _prune_sessions(self, sessions, now):
        for digest in [d for d, s in sessions.items() if self._expired(s, now)]:
            del sessions[digest]

    def session_user(self, token):
        """-> (user, digest) for a live session token, else None."""
        if not valid_token(token):
            return None
        digest = token_digest(token)
        session = self.store.read(SESSIONS, EMPTY_SESSIONS)["sessions"].get(digest)
        now = self.clock()
        if session is None or self._expired(session, now):
            return None
        user = self.get_user(session.get("email"))
        if user is None or user.get("disabled"):
            return None
        if now - session.get("seen", 0) > SEEN_GRANULARITY:
            def bump(doc):
                live = doc["sessions"].get(digest)
                if live is not None:  # revoked meanwhile: leave it revoked
                    live["seen"] = now
            self.store.update(SESSIONS, EMPTY_SESSIONS, bump)
        return user, digest

    def revoke_session(self, digest):
        def change(doc):
            doc["sessions"].pop(digest, None)
        self.store.update(SESSIONS, EMPTY_SESSIONS, change)

    def revoke_user_sessions(self, email):
        def change(doc):
            for d in [d for d, s in doc["sessions"].items() if s.get("email") == email]:
                del doc["sessions"][d]
        self.store.update(SESSIONS, EMPTY_SESSIONS, change)

    # --- invites and resets ---------------------------------------------

    def invites(self):
        return self.store.read(INVITES, EMPTY_INVITES)["invites"]

    def create_invite(self, email, *, kind="invite", admin=False, handle=None, by=None):
        """-> token. Supersedes any pending link of the same kind for `email`."""
        email = normalize_email(email)
        if not email:
            raise AuthError("Enter a valid email address.")
        now = self.clock()
        token, digest = new_token()

        with self.store.locked():
            users = self.store.read(USERS, EMPTY_USERS, fresh=True)["users"]
            doc = self.store.read(INVITES, EMPTY_INVITES, fresh=True)
            invites = doc["invites"]
            if kind == "invite":
                if email in users:
                    raise AuthError(f"{email} already has an account; send a reset link instead.")
                handle = (handle or default_handle(email)).strip().lower()
                if not HANDLE_RE.fullmatch(handle):
                    raise AuthError("Handles are 1-32 lowercase letters, digits, '.', '_' or '-'.")
                taken = {u.get("handle") for u in users.values()}
                taken |= {i.get("handle") for i in invites.values()
                          if i.get("kind") == "invite" and i.get("used") is None
                          and i.get("email") != email and i.get("expires", 0) > now}
                if handle in taken:
                    raise AuthError(f"The handle {handle} is taken; choose another.")
                ttl = INVITE_TTL
            elif kind == "reset":
                if email not in users:
                    raise AuthError(f"No account for {email}.")
                handle, admin, ttl = None, False, RESET_TTL
            else:
                raise AuthError("unknown link kind")
            for d in [d for d, i in invites.items()
                      if i.get("email") == email and i.get("kind") == kind
                      and i.get("used") is None]:
                del invites[d]
            for d in [d for d, i in invites.items() if i.get("expires", 0) < now - 30 * 86400]:
                del invites[d]
            invites[digest] = {"email": email, "kind": kind, "admin": bool(admin),
                               "handle": handle, "created": now, "expires": now + ttl,
                               "created_by": by, "used": None}
            self.store.write(INVITES, doc)
        self.audit(f"{kind}_created", email=email, by=by, invite=digest[:16])
        return token

    def _live_invite(self, invites, token, now):
        if not valid_token(token):
            return None, None
        digest = token_digest(token)
        invite = invites.get(digest)
        if invite is None or invite.get("used") is not None or invite.get("expires", 0) <= now:
            return None, None
        return digest, invite

    def invite_info(self, token):
        """-> {email, kind, handle} for a usable link, else None. Never consumes it."""
        _, invite = self._live_invite(self.invites(), token, self.clock())
        if invite is None:
            return None
        return {"email": invite["email"], "kind": invite["kind"],
                "handle": invite.get("handle")}

    def accept_invite(self, token, password):
        """Set the password a link was issued for. -> the account's email.

        The link is checked before hashing (cheap refusals stay cheap) and
        again under the lock, so a link can be used exactly once even when
        two requests race.
        """
        if self.invite_info(token) is None:
            raise AuthError("This link is invalid, expired, or already used.")
        problem = password_problem(password)
        if problem:
            raise AuthError(problem)
        hashed = self._hashing(hash_password, password, self.scrypt_n)
        now = self.clock()
        with self.store.locked():
            inv_doc = self.store.read(INVITES, EMPTY_INVITES, fresh=True)
            digest, invite = self._live_invite(inv_doc["invites"], token, now)
            if invite is None:
                raise AuthError("This link is invalid, expired, or already used.")
            users_doc = self.store.read(USERS, EMPTY_USERS, fresh=True)
            users = users_doc["users"]
            email = invite["email"]
            if invite["kind"] == "invite":
                if email in users:
                    raise AuthError("This account already exists; ask for a reset link.")
                if invite.get("handle") in {u.get("handle") for u in users.values()}:
                    raise AuthError("This link's handle is now taken; ask for a new invite.")
                users[email] = {"handle": invite["handle"], "admin": bool(invite.get("admin")),
                                "disabled": False, "password": hashed,
                                "created": now, "password_changed": now}
            else:
                user = users.get(email)
                if user is None:
                    raise AuthError("This account no longer exists.")
                user["password"] = hashed
                user["password_changed"] = now
            # Users first: a crash between the writes leaves a usable link to
            # an account that already exists, which refuses cleanly — never a
            # consumed link with no account behind it.
            self.store.write(USERS, users_doc)
            invite["used"] = now
            self.store.write(INVITES, inv_doc)
            self.revoke_user_sessions(email)
        self.audit("password_set", email=email, kind=invite["kind"], invite=digest[:16])
        return email

    def revoke_invite(self, invite_id, by=None):
        def change(doc):
            matches = [d for d in doc["invites"] if d.startswith(invite_id)]
            if len(invite_id) < 16 or len(matches) != 1:
                raise AuthError("no such pending link")
            del doc["invites"][matches[0]]
            return matches[0]
        digest = self.store.update(INVITES, EMPTY_INVITES, change)
        self.audit("invite_revoked", invite=digest[:16], by=by)

    # --- admin view -----------------------------------------------------

    def overview(self):
        now = self.clock()
        sessions = self.store.read(SESSIONS, EMPTY_SESSIONS)["sessions"].values()
        last_seen = {}
        for s in sessions:
            if not self._expired(s, now):
                last_seen[s["email"]] = max(last_seen.get(s["email"], 0), s["seen"])
        users = [{"email": e, "handle": u.get("handle"), "admin": bool(u.get("admin")),
                  "disabled": bool(u.get("disabled")), "created": u.get("created"),
                  "lastSeen": last_seen.get(e)}
                 for e, u in sorted(self.users().items())]
        invites = [{"id": d[:16], "email": i["email"], "kind": i["kind"],
                    "handle": i.get("handle"), "admin": bool(i.get("admin")),
                    "created": i["created"], "expires": i["expires"]}
                   for d, i in sorted(self.invites().items(), key=lambda kv: kv[1]["created"])
                   if i.get("used") is None and i.get("expires", 0) > now]
        return {"users": users, "invites": invites}
