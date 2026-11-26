"""qda_admin — manage hosted-mode accounts and projects from this machine.

    python3 app.py admin init --public-origin https://HOST [--data-root DIR] [--audio-root DIR]
    python3 app.py admin invite EMAIL [--admin] [--handle H] [--project SLUG ...]
    python3 app.py admin reset EMAIL
    python3 app.py admin disable EMAIL | enable EMAIL | revoke-sessions EMAIL
    python3 app.py admin project add SLUG --name NAME --data DIR --audio DIR
    python3 app.py admin project grant SLUG EMAIL | revoke SLUG EMAIL
    python3 app.py admin list

Invite and reset print a one-time link; send it to the person yourself.
Works whether or not the server is running: both write through the same lock.
"""
from __future__ import annotations

import argparse
import datetime
import sys
import time
import urllib.parse
from pathlib import Path

import qda_auth
from qda_projects import ProjectError, Registry
from qda_store import StateDirError, Store, default_state_dir, ensure_private_dir

ROOT = Path(__file__).resolve().parent


def _when(ts):
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _open(state_dir, create=False):
    try:
        path = ensure_private_dir(state_dir, create=create)
    except StateDirError as e:
        raise SystemExit(str(e))
    try:
        path.resolve().relative_to(ROOT)
        raise SystemExit(f"{path} is inside the repository; choose a state directory "
                         f"outside it (QDA_STATE_DIR or --state-dir).")
    except ValueError:
        pass
    return Store(path)


def _config(store):
    config = store.read("config.json", {})
    if not config.get("public_origin"):
        raise SystemExit("Run `python3 app.py admin init --public-origin https://HOST` first.")
    return config


def _link(store, token):
    return f"{_config(store)['public_origin']}/invite#{token}"


def cmd_init(args, store):
    parts = urllib.parse.urlsplit(args.public_origin)
    if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/"):
        raise SystemExit("--public-origin must look like https://host.tailnet.ts.net")
    origin = f"https://{parts.netloc.lower()}"

    def change(config):
        config["version"] = 1
        config["public_origin"] = origin
        for key, values in (("data_roots", args.data_root), ("audio_roots", args.audio_root)):
            if values:
                resolved = []
                for value in values:
                    p = Path(value).expanduser()
                    if not p.is_absolute() or not p.is_dir():
                        raise SystemExit(f"{value} is not an existing absolute directory")
                    resolved.append(str(p.resolve()))
                config[key] = resolved
            config.setdefault(key, [])
    store.update("config.json", {}, change)
    print(f"state directory: {store.dir}")
    print(f"public origin:   {origin}")


def cmd_invite(args, store):
    auth = qda_auth.Auth(store)
    registry = Registry(store)
    records = registry.records()
    for slug in args.project or []:
        if slug not in records:
            raise SystemExit(f"no project {slug}; create it with `admin project add`")
    try:
        token = auth.create_invite(args.email, admin=args.admin, handle=args.handle, by="cli")
    except qda_auth.AuthError as e:
        raise SystemExit(str(e))
    email = qda_auth.normalize_email(args.email)
    for slug in args.project or []:
        registry.set_member(slug, email, True)
        auth.audit("member_added", project=slug, email=email, by="cli")
    print(f"Invite for {email} (valid 7 days, single use). Send them:\n\n  {_link(store, token)}\n")


def cmd_reset(args, store):
    auth = qda_auth.Auth(store)
    try:
        token = auth.create_invite(args.email, kind="reset", by="cli")
    except qda_auth.AuthError as e:
        raise SystemExit(str(e))
    print(f"Password reset link (valid 24 hours, single use). Send them:\n\n"
          f"  {_link(store, token)}\n")


def _set_disabled(store, email, disabled):
    auth = qda_auth.Auth(store)
    email = qda_auth.normalize_email(email) or email
    try:
        auth.set_disabled(email, disabled, by="cli")
    except qda_auth.AuthError as e:
        raise SystemExit(str(e))
    print(f"{email}: {'disabled; signed out everywhere' if disabled else 'enabled'}")


def cmd_disable(args, store):
    _set_disabled(store, args.email, True)


def cmd_enable(args, store):
    _set_disabled(store, args.email, False)


def cmd_revoke_sessions(args, store):
    auth = qda_auth.Auth(store)
    email = qda_auth.normalize_email(args.email) or args.email
    auth.revoke_user_sessions(email)
    auth.audit("sessions_revoked", email=email, by="cli")
    print(f"{email}: signed out everywhere")


def cmd_project(args, store):
    registry = Registry(store)
    auth = qda_auth.Auth(store)
    try:
        if args.action == "add":
            rec = registry.add(args.slug, args.name, args.data, args.audio, now=time.time(),
                               protected=[ROOT, store.dir],
                               forbidden_inside=[ROOT / "static", ROOT / "pages",
                                                 ROOT / ".git", store.dir])
            auth.audit("project_created", project=args.slug, data_dir=rec["data_dir"],
                       audio_dir=rec["audio_dir"], by="cli")
            print(f"project {args.slug}: data {rec['data_dir']}, audio {rec['audio_dir']}")
            if Path(rec["audio_dir"]).is_relative_to(rec["data_dir"]):
                print("note: the audio directory is inside the data directory; make sure "
                      "it is gitignored if the data directory is a git repository.")
        else:
            email = qda_auth.normalize_email(args.email)
            if not email:
                raise SystemExit("not a valid email address")
            registry.set_member(args.slug, email, args.action == "grant")
            auth.audit("member_added" if args.action == "grant" else "member_removed",
                       project=args.slug, email=email, by="cli")
            print(f"{email} {'added to' if args.action == 'grant' else 'removed from'} {args.slug}")
    except ProjectError as e:
        raise SystemExit(str(e))


def cmd_list(args, store):
    auth = qda_auth.Auth(store)
    config = store.read("config.json", {})
    overview = auth.overview()
    print(f"state directory: {store.dir}")
    print(f"public origin:   {config.get('public_origin', '(not set — run admin init)')}")
    print("\naccounts:")
    for u in overview["users"] or []:
        flags = ", ".join(f for f, on in (("admin", u["admin"]), ("DISABLED", u["disabled"])) if on)
        print(f"  {u['email']:<36} {u['handle']:<16} {flags:<16} last seen {_when(u['lastSeen'])}")
    if not overview["users"]:
        print("  (none)")
    print("\npending links:")
    for i in overview["invites"]:
        print(f"  {i['id']}  {i['kind']:<6} {i['email']:<36} expires {_when(i['expires'])}")
    if not overview["invites"]:
        print("  (none)")
    print("\nprojects:")
    records = Registry(store).records()
    for slug, rec in sorted(records.items()):
        print(f"  {slug}: {rec.get('name')}\n    data  {rec['data_dir']}\n    audio {rec['audio_dir']}")
        print(f"    members: {', '.join(rec.get('members', [])) or '(none)'}")
    if not records:
        print("  (none)")


def main(argv):
    parser = argparse.ArgumentParser(prog="app.py admin",
                                     description="Manage hosted-mode accounts and projects.")
    parser.add_argument("--state-dir", default=None,
                        help="default: $QDA_STATE_DIR or ~/.local/share/code-qda")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create the state directory and set the public URL")
    p.add_argument("--public-origin", required=True, help="e.g. https://host.tailnet.ts.net")
    p.add_argument("--data-root", action="append",
                   help="directory under which the admin page may register project data")
    p.add_argument("--audio-root", action="append",
                   help="directory under which the admin page may register project audio")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("invite", help="print a one-time sign-up link for an email")
    p.add_argument("email")
    p.add_argument("--admin", action="store_true", help="make this account a site admin")
    p.add_argument("--handle", help="short name recorded in transcript edit logs")
    p.add_argument("--project", action="append", help="add them to this project (repeatable)")
    p.set_defaults(fn=cmd_invite)

    p = sub.add_parser("reset", help="print a one-time password reset link")
    p.add_argument("email")
    p.set_defaults(fn=cmd_reset)

    for name, fn, help_text in (("disable", cmd_disable, "block sign-in and sign out everywhere"),
                                ("enable", cmd_enable, "allow sign-in again"),
                                ("revoke-sessions", cmd_revoke_sessions, "sign out everywhere")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("email")
        p.set_defaults(fn=fn)

    p = sub.add_parser("project", help="create projects and manage members")
    psub = p.add_subparsers(dest="action", required=True)
    pa = psub.add_parser("add", help="register a project's data and audio directories")
    pa.add_argument("slug", help="URL id, e.g. robotics")
    pa.add_argument("--name", required=True)
    pa.add_argument("--data", required=True, help="absolute path of the transcripts directory")
    pa.add_argument("--audio", required=True, help="absolute path of the audio directory")
    for action in ("grant", "revoke"):
        pg = psub.add_parser(action, help=f"{action} a person's membership")
        pg.add_argument("slug")
        pg.add_argument("email")
    p.set_defaults(fn=cmd_project)

    p = sub.add_parser("list", help="show accounts, pending links and projects")
    p.set_defaults(fn=cmd_list)

    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    store = _open(state_dir, create=args.command == "init")
    args.fn(args, store)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
