# Hosting code-qda for collaborators

Collaborators can use code-qda in a browser without installing anything.
This machine runs it, and [Tailscale Funnel](https://tailscale.com/kb/1223/funnel)
publishes it at an HTTPS address. Nobody can create an account: you invite
people by email. Each study is a separate **project** with its own transcripts,
codebook and audio, and a person sees only the projects you add them to.

This guide covers setup, day-to-day administration, and what hosting does and
does not protect.

## How it fits together

```
browser ──HTTPS──▶ Tailscale Funnel (TLS ends here, on this machine)
                       │  unix socket, /run/user/<uid>/code-qda/http.sock
                       ▼
                 python3 app.py --hosted
                   ├─ state (accounts, sessions, projects)  ~/.local/share/code-qda/   0700
                   └─ projects
                        robotics → md/ (transcripts, codes)  +  md/audio/ (IRB audio)
```

- **Accounts.** An account is an email address and a password. You create an
  invite, which is a one-time link. You email it yourself; the app sends no
  email. Following the link lets the person choose a password.
- **Password resets.** These use the same kind of link. Invite links last
  7 days and reset links 24 hours.
- **Projects.** A project is a directory of transcripts (`.md`, `.tx.json`,
  `.annot.json`, `codes.json`) plus a directory of audio. Members get
  everything in the project. To anyone else, the project doesn't exist: they
  get the same 404 as for a made-up name.
- **Edit history.** Every transcript correction records the editor's
  **handle**, taken from their session. It is a short name like `rolando`, not
  their email, so emails stay out of edit logs.
- **State directory.** Accounts and sessions live in a state directory outside
  the repository, so they can never be committed. Passwords are stored only as
  scrypt hashes, and tokens only as SHA-256 digests.

## One-time setup

The commands below use this machine's tailnet name. Substitute yours if it
differs (`tailscale status --json | grep DNSName`).

```sh
HOST=rg-legion-t7-34imz5.tail2c9735.ts.net
cd ~/git/code-qda
```

### 1. Tailscale

1. In the Tailscale admin console, open **DNS** and enable **HTTPS
   Certificates**.
2. In **Access controls**, allow this machine to use Funnel. Add a node
   attribute, with your own login or a tag that is on this machine as the
   target:

   ```json
   "nodeAttrs": [
     {"target": ["you@example.com"], "attr": ["funnel"]}
   ]
   ```

3. Let your account run `tailscale serve` and `tailscale funnel` without
   sudo:

   ```sh
   sudo tailscale set --operator=$USER
   ```

**The hostname becomes public.** Its TLS certificate is published in
Certificate Transparency logs, and scanners will find it within hours. Finish
and verify step 5 before you turn on Funnel.

### 2. Lock down the data on disk

The transcripts and audio are IRB-controlled. Make sure no other account on
this machine can read them through the file system:

```sh
chmod 700 md md/audio
chmod 600 md/*.json md/*.md
```

### 3. Accounts and the first project

```sh
python3 app.py admin init --public-origin https://$HOST
python3 app.py admin project add robotics --name "Robotics interview study" \
    --data "$PWD/md" --audio "$PWD/md/audio"
python3 app.py admin invite you@example.edu --admin --handle rolando --project robotics
```

The last command prints your own invite link; keep it for step 5.

**Optional:** to create projects from the admin page instead of the command
line, add `--data-root DIR --audio-root DIR` to `admin init`. The web page can
only register directories under those roots, so a stolen admin password can't
point a project at arbitrary files.

### 4. Run it as a service

The service runs as your user, starts at boot, and keeps running after you
log out:

```sh
mkdir -p ~/.config/systemd/user
cp deploy/code-qda.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now code-qda
sudo loginctl enable-linger $USER
systemctl --user status code-qda        # "code-qda (hosted, https://…) → unix:…"
```

The unit runs `/usr/bin/python3`, and the repository path is set inside
[`deploy/code-qda.service`](../deploy/code-qda.service). Edit the unit if
either differs on your machine.

### 5. Stage it on your tailnet first

`serve` makes the app reachable only by devices on your tailnet, at the same
URL that Funnel will use later:

```sh
tailscale serve --bg unix:/run/user/$(id -u)/code-qda/http.sock
```

On another device on the tailnet, open your invite link, set a password, and
check that the robotics project opens. Then work through
[the verification checklist](#verification-checklist).

### 6. Open it to the internet

```sh
tailscale serve reset
tailscale funnel --bg unix:/run/user/$(id -u)/code-qda/http.sock
tailscale funnel status
```

Then invite your collaborators (see [Inviting people](#inviting-people)).

## Verification checklist

Run these from a device **off** your tailnet, such as a phone on cellular
data. `curl` here means any machine that isn't on the tailnet.

```sh
H=https://rg-legion-t7-34imz5.tail2c9735.ts.net
curl -si $H/ | head -3                       # 303 → /login?next=%2F
curl -si $H/p/robotics/api/files | head -1   # 401
diff <(curl -s -D- $H/p/robotics/api/files | grep -v '^date') \
     <(curl -s -D- $H/p/nope/api/files | grep -v '^date')   # no output: identical
for p in /api/files /.git/config /md/codes.json /static/../app.py; do
  curl -s -o /dev/null -w "%{http_code} $p\n" $H$p; done   # all 4xx, never 200
curl -sI $H/login | grep -iE 'content-security|strict-transport|x-content-type'
curl -si -X POST $H/auth/login -H 'Origin: https://evil.example' \
     -H 'Content-Type: application/json' -d '{}' | head -1   # 403
```

In a browser:

- **Session cookie.** In developer tools, under **Application › Cookies**, the
  only cookie is `__Host-qda_session`, marked Secure, HttpOnly and
  SameSite=Lax.
- **Console.** It shows no Content-Security-Policy errors.
- **Audio.** It plays and seeks (Verify against audio).
- **Invite links.** The address bar loses the `#…` token as soon as the
  invite page loads.
- **Sign-out.** Signing out in one tab sends other tabs to sign-in on their
  next action.
- **Isolation.** Signed in as an editor, the URL of a project you're not in
  says not found.
- **Throttling.** The 6th wrong password in a row for one email gets
  "Too many attempts".

On this machine:

```sh
ss -ltnp | grep python                  # nothing: no TCP port is open
ss -lxp | grep code-qda                 # the unix socket
stat -c '%a %n' ~/.local/share/code-qda ~/.local/share/code-qda/*   # 700 / 600
journalctl --user -u code-qda | grep -iE 'token|password'           # no output
tail ~/.local/share/code-qda/audit.log   # logins, invites, writes
```

## Inviting people

From the **Admin** page (the link next to your name, or `/admin`):

1. Under **Invite someone**, enter their email and tick their projects.
   **Handle** is optional; it defaults to the part of the email before the @.
2. Click **Create invite link** and copy the link. It is shown only once.
3. Email it to them. They open it, choose a password of at least 12
   characters, and land in their project.

The same tasks from the command line:

```sh
python3 app.py admin invite ana@example.edu --project robotics
python3 app.py admin project grant robotics ana@example.edu
python3 app.py admin project revoke robotics ana@example.edu
python3 app.py admin reset ana@example.edu          # forgot their password
python3 app.py admin disable ana@example.edu        # leaves the study: signed out everywhere
python3 app.py admin list
```

Membership changes take effect on the person's very next request. Disabling
an account, resetting its password, or **Sign out** on the admin page ends all
of that person's sessions immediately.

A new project needs its two directories to exist first:

```sh
python3 app.py admin project add study2 --name "Study 2" --data /abs/path/study2/md --audio /abs/path/study2/audio
```

## Running it

| Task | Command |
|---|---|
| Logs | `journalctl --user -u code-qda -f` |
| Restart after `git pull` | `systemctl --user restart code-qda` |
| **Take it offline now** | `tailscale funnel reset` (the tailnet keeps access through `serve`, if enabled) |
| Stop everything | `tailscale funnel reset; systemctl --user stop code-qda` |
| Who did what | `~/.local/share/code-qda/audit.log` (JSON lines: logins, failures, invites, membership, every write) |

**Backups.** Back up two things, to private storage:

- each project's data directory, which holds the coding work;
- the state directory, which holds password hashes and sessions.

Never put the state directory in git or a shared folder.

**Sharing the data through git.** `md/` is no longer tracked by this
repository: code and data are separate. With the hosted app, the server's copy
of a project is the one everyone edits. If you want version history, make the
project's data directory its own private git repository. Commit from the
server only, for example a daily `git -C md add -A && git -C md commit -m
snapshot`. Don't let people push changes to the same files from their own
copies, or the two will diverge. Audio stays out of every repository.

**Editing at the same time.** Several people can code at once:

- Codes they create merge; nobody's new code is erased by someone else's
  stale tab.
- If two people change the codings of the *same transcript* at the same time,
  the second save is refused. That person's view reloads with a notice to
  redo their last change, rather than silently overwriting the first person's
  work.

## What this protects against, and what it doesn't

Hosted mode is designed for these threats:

- **Anyone on the internet.** Every page and API requires a session, except
  sign-in, invite and static code.
- **Password guessing.** Login attempts are throttled per email, per client
  address and overall. Hashing is scrypt, with a cap on concurrent hashes so a
  login flood can't exhaust memory.
- **Other websites acting through a signed-in browser (CSRF).** Every
  state-changing request must carry this site's `Origin` and a JSON body. The
  cookie is `SameSite=Lax`, `HttpOnly`, `Secure` and `__Host-` prefixed.
- **Injected markup or script.** A strict Content-Security-Policy (no inline
  script) and server-side validation of code colors and ids keep one account
  from injecting markup into another's view.
- **One collaborator reaching another study's data.** Membership is checked
  before any file is touched, and a request can only name a file within the
  project's directories.
- **Misconfiguration.** Local mode (`python3 app.py`) has no login, so it
  answers only `localhost` and refuses any request that came through a proxy.
  Pointing Funnel at it by mistake fails closed.

These are not covered:

- **Other administrators of this machine.** Every account in the `docker` or
  `sudo` group (four accounts at the time of writing) can read anything on
  disk, including the transcripts, audio, and password hashes. Bring this to
  your IRB data-security plan; the app can't change it.
- **Tailscale.** It terminates TLS and could in principle see traffic. That is
  inherent to Funnel.
- **Phishing.** There is no second factor. Keep the number of admins small,
  and disable accounts when people leave the study.
- **What members do with what they can see.** An editor can read and play
  everything in their projects.

**IRB checklist before inviting anyone:**

1. Confirm the protocol allows authenticated remote access to transcripts and
   audio.
2. Confirm it allows data at rest on this shared host.
3. Make each project's members match the approved personnel list.
4. Keep the audit log.
5. Disable accounts when people leave.

## Troubleshooting

- **Every page says 421 Misdirected Request.** The `Host` header reaching the
  app isn't the public hostname. Add the value you see to `config.json` as
  `"allowed_hosts": ["…"]` in the state directory, then restart.
- **Sign-in seems to work but you land back on sign-in.** You opened the app
  by a different address than the public one, for example
  `http://127.0.0.1`. The session cookie only works over HTTPS at the public
  URL.
- **"Refusing to start: … accessible to other users".** Run the `chmod` it
  prints. The state directory must be 0700 and its files 0600.
- **Audio isn't available.** The audio file named in the transcript isn't in
  the project's audio directory. Its name must match the `audio` field of the
  `.tx.json`.
