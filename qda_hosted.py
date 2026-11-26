"""qda_hosted — the signed-in front door for hosted mode.

Mixed into app.Handler. Every request in hosted mode is routed here first;
the order of checks is the security model, so it reads top to bottom:

    Host header      (already checked by Handler._precheck)
    public routes    /static/*, /login, /invite, the three /auth POSTs
    session          from the __Host-qda_session cookie, or 401 / redirect
    authorization    /admin* for admins only; /p/<slug>/* for members only
    route            the project's data API (app.PROJECT_ROUTES)

A project that does not exist and a project the user is not a member of
produce the same empty 404, before any filesystem access.
"""
from __future__ import annotations

import re
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGES_DIR = ROOT / "pages"
STATIC_DIR = ROOT / "static"
COOKIE = "__Host-qda_session"
COOKIE_RE = re.compile(r"(?:^|;)\s*__Host-qda_session=([A-Za-z0-9_-]{43})\s*(?=;|$)")
PROJECT_PATH = re.compile(r"/p/([^/]*)(/.*)?")
MAX_AUTH_BODY = 8 * 1024
MAX_ADMIN_BODY = 64 * 1024

AUTH_POSTS = {
    "/auth/login": "_login",
    "/auth/invite/info": "_invite_info",
    "/auth/invite/accept": "_invite_accept",
    "/auth/logout": "_logout",
}
ADMIN_POSTS = {
    "/admin/api/invite": "_admin_invite",
    "/admin/api/reset": "_admin_reset",
    "/admin/api/invite/revoke": "_admin_revoke_invite",
    "/admin/api/user": "_admin_user",
    "/admin/api/project": "_admin_register_project",
    "/admin/api/project/create": "_admin_create_project",
    "/admin/api/members": "_admin_members",
}

SIGNED_OUT = "Sign in to continue."
BAD_LOGIN = "That email and password don't match an account."
BAD_LINK = "This link is invalid, expired, or already used."
THROTTLED = "Too many attempts. Wait 15 minutes and try again."
BUSY = "The server is busy. Try again in a moment."


def safe_next(value):
    """A same-site path to continue to after signing in, else '/'."""
    if (not isinstance(value, str) or not value.startswith("/")
            or value.startswith("//") or value.startswith("/\\")
            or any(ord(c) < 33 or c == "\\" for c in value) or len(value) > 2000):
        return "/"
    return value


class HostedMixin:
    # --- cookies ----------------------------------------------------------

    def _session_token(self):
        for header in self.headers.get_all("Cookie") or []:
            match = COOKIE_RE.search(header)
            if match:
                return match.group(1)
        return None

    def _session_cookie(self, token):
        from qda_auth import SESSION_ABSOLUTE
        return ("Set-Cookie", f"{COOKIE}={token}; Path=/; Secure; HttpOnly; "
                              f"SameSite=Lax; Max-Age={SESSION_ABSOLUTE}")

    def _clear_cookie(self):
        return ("Set-Cookie", f"{COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0")

    # --- routing ----------------------------------------------------------

    def _dispatch_hosted(self, path, params, query):
        app = self.server.app
        cmd = self.command

        if path.startswith("/static/"):
            if cmd == "GET":
                self._try_serve_static(path)
            else:
                self._status(404)
            return
        if cmd == "POST" and path in AUTH_POSTS and path != "/auth/logout":
            getattr(self, AUTH_POSTS[path])()
            return

        found = app.auth.session_user(self._session_token())
        if found:
            self.user, self.session_digest = found

        if cmd == "GET" and path in ("/login", "/invite"):
            if path == "/login" and self.user:
                self._redirect(303, safe_next(params.get("next", ["/"])[0]))
            else:
                self._serve_page(path[1:] + ".html")
            return
        if cmd == "POST" and path == "/auth/logout":
            self._logout()
            return
        if cmd == "GET" and path == "/auth/me":
            if self.user is None:
                self._json({"error": SIGNED_OUT}, status=401)
            else:
                self._json(self._me())
            return

        if self.user is None:
            if cmd == "GET" and "/api/" not in path:
                target = path + ("?" + query if query else "")
                self._redirect(303, "/login?next=" + urllib.parse.quote(target, safe=""))
            else:
                self._json({"error": SIGNED_OUT}, status=401)
            return

        if path == "/" and cmd == "GET":
            self._home()
            return
        if path == "/admin" or path.startswith("/admin/"):
            if not self.user.get("admin"):
                self._status(404)
            else:
                self._admin_route(path)
            return
        match = PROJECT_PATH.fullmatch(path)
        if match:
            project = app.registry.for_member(match.group(1), self.user["email"])
            if project is None:
                self._status(404)
                return
            rest = match.group(2)
            if rest is None:
                self._redirect(308, f"/p/{project.slug}/" + ("?" + query if query else ""))
            elif rest in ("/", "/index.html") and cmd == "GET":
                self._serve_index()
            elif rest.startswith("/api/"):
                self._project_route(project, rest, params)
            else:
                self._status(404)
            return
        self._status(404)

    def _serve_page(self, name):
        self._serve_file(PAGES_DIR / name, "text/html; charset=utf-8")

    def _me(self):
        app = self.server.app
        return {"mode": "hosted", "email": self.user["email"],
                "handle": self.user.get("handle"), "admin": bool(self.user.get("admin")),
                "projects": app.registry.projects_for(self.user["email"])}

    def _home(self):
        projects = self.server.app.registry.projects_for(self.user["email"])
        if len(projects) == 1 and not self.user.get("admin"):
            self._redirect(303, f"/p/{projects[0]['slug']}/")
        else:
            self._serve_page("projects.html")

    def _audit_write(self, route, params):
        app = self.server.app
        if app.mode != "hosted" or self.user is None:
            return
        name = params.get("name", [None])[0]
        app.auth.audit("write", email=self.user["email"], project=self.project.slug,
                       route=route, file=name)

    # --- sign-in ------------------------------------------------------------

    def _limit_keys(self, kind, email=None):
        auth = self.server.app.auth
        ip = self.client_ip()
        if kind == "login":
            keys = [(auth.limit_email, "email:" + (email or "")),
                    (auth.limit_global, "global")]
            if ip:
                keys.append((auth.limit_ip, "ip:" + ip))
        else:
            keys = [(auth.limit_tokens, "tokens:" + (ip or "global"))]
        return keys

    def _login(self):
        from qda_auth import Busy, normalize_email, token_digest
        body = self._write_body(MAX_AUTH_BODY)
        if body is None:
            return
        auth = self.server.app.auth
        email = normalize_email(body.get("email")) or ""
        keys = self._limit_keys("login", email)
        if any(limiter.blocked(key) for limiter, key in keys):
            auth.audit("login_throttled", email=email or None, ip=self.client_ip())
            self._json({"error": THROTTLED}, status=429)
            return
        try:
            user = auth.authenticate(email, body.get("password"))
        except Busy:
            self._json({"error": BUSY}, status=503, headers=[("Retry-After", "5")])
            return
        if user is None:
            for limiter, key in keys:
                limiter.fail(key)
            auth.audit("login_failed", email=email or None, ip=self.client_ip())
            self._json({"error": BAD_LOGIN}, status=401)
            return
        auth.limit_email.clear("email:" + email)
        old = self._session_token()
        if old:
            auth.revoke_session(token_digest(old))
        token = auth.create_session(user["email"])
        auth.audit("login", email=user["email"], ip=self.client_ip())
        self._json({"ok": True, "next": safe_next(body.get("next"))},
                   headers=[self._session_cookie(token)])

    def _invite_info(self):
        body = self._write_body(MAX_AUTH_BODY)
        if body is None:
            return
        auth = self.server.app.auth
        keys = self._limit_keys("token")
        if any(limiter.blocked(key) for limiter, key in keys):
            self._json({"error": THROTTLED}, status=429)
            return
        info = auth.invite_info(body.get("token"))
        if info is None:
            for limiter, key in keys:
                limiter.fail(key)
            self._json({"error": BAD_LINK}, status=404)
            return
        self._json(info)

    def _invite_accept(self):
        from qda_auth import AuthError, Busy
        body = self._write_body(MAX_AUTH_BODY)
        if body is None:
            return
        auth = self.server.app.auth
        keys = self._limit_keys("token")
        if any(limiter.blocked(key) for limiter, key in keys):
            self._json({"error": THROTTLED}, status=429)
            return
        try:
            email = auth.accept_invite(body.get("token"), body.get("password"))
        except AuthError as e:
            if str(e) == BAD_LINK:
                for limiter, key in keys:
                    limiter.fail(key)
            self._json({"error": str(e)}, status=400)
            return
        except Busy:
            self._json({"error": BUSY}, status=503, headers=[("Retry-After", "5")])
            return
        if self._session_token():
            # Whoever was signed in on this browser before is signed out,
            # rather than left with an orphaned session.
            from qda_auth import token_digest
            auth.revoke_session(token_digest(self._session_token()))
        token = auth.create_session(email)
        auth.audit("login", email=email, ip=self.client_ip(), via="invite")
        self._json({"ok": True, "next": "/"}, headers=[self._session_cookie(token)])

    def _logout(self):
        body = self._write_body(MAX_AUTH_BODY)
        if body is None:
            return
        auth = self.server.app.auth
        if self.session_digest:
            auth.revoke_session(self.session_digest)
            auth.audit("logout", email=self.user["email"])
        self._json({"ok": True}, headers=[self._clear_cookie()])

    # --- admin --------------------------------------------------------------

    def _admin_route(self, path):
        if self.command == "GET" and path == "/admin":
            self._serve_page("admin.html")
        elif self.command == "GET" and path == "/admin/api/state":
            self._json(self._admin_state())
        elif self.command == "POST" and path in ADMIN_POSTS:
            from qda_auth import AuthError
            from qda_projects import ProjectError
            body = self._write_body(MAX_ADMIN_BODY)
            if body is None:
                return
            try:
                result = getattr(self, ADMIN_POSTS[path])(body)
            except (AuthError, ProjectError) as e:
                self._json({"error": str(e)}, status=400)
                return
            state = self._admin_state()
            state.update(result or {})
            self._json(state)
        else:
            self._status(404)

    def _admin_state(self):
        app = self.server.app
        state = app.auth.overview()
        state["projects"] = [
            {"slug": slug, "name": rec.get("name") or slug, "dataDir": rec["data_dir"],
             "audioDir": rec["audio_dir"], "storage": rec.get("storage", "registered"),
             "members": rec.get("members", [])}
            for slug, rec in sorted(app.registry.records().items())]
        state["roots"] = app.roots or {"data": [], "audio": []}
        state["me"] = self.user["email"]
        return state

    def _admin_email(self, body, key="email"):
        from qda_auth import AuthError, normalize_email
        email = normalize_email(body.get(key))
        if not email:
            raise AuthError("Enter a valid email address.")
        return email

    def _link(self, token):
        return f"{self.server.app.public_origin}/invite#{token}"

    def _admin_invite(self, body):
        from qda_auth import AuthError
        app = self.server.app
        email = self._admin_email(body)
        slugs = body.get("projects") or []
        records = app.registry.records()
        if not isinstance(slugs, list) or any(s not in records for s in slugs):
            raise AuthError("Choose projects from the list.")
        handle = body.get("handle") or None
        if handle is not None and not isinstance(handle, str):
            raise AuthError("Handles are text.")
        token = app.auth.create_invite(email, admin=body.get("admin") is True,
                                       handle=handle, by=self.user["email"])
        for slug in slugs:
            app.registry.set_member(slug, email, True)
            app.auth.audit("member_added", project=slug, email=email, by=self.user["email"])
        return {"link": self._link(token), "linkEmail": email}

    def _admin_reset(self, body):
        email = self._admin_email(body)
        token = self.server.app.auth.create_invite(email, kind="reset", by=self.user["email"])
        return {"link": self._link(token), "linkEmail": email}

    def _admin_revoke_invite(self, body):
        from qda_auth import AuthError
        invite_id = body.get("id")
        if not isinstance(invite_id, str) or not re.fullmatch(r"[0-9a-f]{16}", invite_id):
            raise AuthError("no such pending link")
        self.server.app.auth.revoke_invite(invite_id, by=self.user["email"])

    def _admin_user(self, body):
        from qda_auth import AuthError
        auth = self.server.app.auth
        email = self._admin_email(body)
        if "disabled" in body:
            if email == self.user["email"]:
                raise AuthError("You can't disable your own account.")
            auth.set_disabled(email, body.get("disabled") is True, by=self.user["email"])
        if body.get("revokeSessions") is True:
            auth.revoke_user_sessions(email)
            auth.audit("sessions_revoked", email=email, by=self.user["email"])

    def _admin_create_project(self, body):
        app = self.server.app
        slug = body.get("slug")
        rec = app.registry.create(slug, body.get("name"), now=time.time())
        self._audit_project(slug, rec)

    def _admin_register_project(self, body):
        app = self.server.app
        slug = body.get("slug")
        rec = app.registry.add(
            slug, body.get("name"), str(body.get("dataDir") or ""),
            str(body.get("audioDir") or ""), now=time.time(),
            protected=[ROOT], forbidden_inside=[STATIC_DIR, PAGES_DIR, ROOT / ".git"],
            roots=app.roots or {"data": [], "audio": []})
        self._audit_project(slug, rec)

    def _audit_project(self, slug, rec):
        self.server.app.auth.audit(
            "project_created", project=slug, storage=rec["storage"],
            data_dir=rec["data_dir"], audio_dir=rec["audio_dir"], by=self.user["email"])

    def _admin_members(self, body):
        app = self.server.app
        email = self._admin_email(body)
        slug = body.get("slug")
        member = body.get("member") is True
        app.registry.set_member(slug, email, member)
        app.auth.audit("member_added" if member else "member_removed",
                       project=slug, email=email, by=self.user["email"])
