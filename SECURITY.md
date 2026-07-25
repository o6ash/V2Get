# Security Policy

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.x     | ✅ |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's [private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(*Security → Report a vulnerability* on the repository), or email the
maintainer listed in the repository profile.

Please include:

- what the issue is and why it matters,
- steps to reproduce (a minimal case is ideal),
- affected version or commit,
- any suggested fix.

You can expect an acknowledgement within **72 hours** and a status update
within **7 days**. Fixes for confirmed issues are released as soon as
practical, and we are happy to credit you in the changelog unless you prefer to
stay anonymous.

---

## The secrets this project handles

v2get holds credentials that are unusually sensitive. Understand each one
before you deploy.

### `TELEGRAM_SESSION` — the highest-value secret

This is a Telethon `StringSession` for a **logged-in user account**, not a bot
token. Anyone who obtains it has **full control of that Telegram account**:
reading all private chats, sending messages as you, and changing settings.

- A bot token cannot replace it — Telegram bots cannot read arbitrary public
  channel history, which is exactly what collection requires.
- **Use a dedicated Telegram account**, not your personal one.
- If it leaks, revoke it immediately: *Telegram → Settings → Devices →
  Terminate Session*, then mint a new one with `python -m app.tools.gen_session`.

### `GITHUB_TOKEN`

A Personal Access Token with write access to the publish repository. Scope it
as narrowly as possible:

- prefer a **fine-grained PAT** limited to the single publish repository with
  *Contents: Read and write* only;
- a classic PAT needs `repo`, which grants access to **all** your repositories
  — avoid it if you can;
- set an expiry date and rotate on schedule;
- revoke at <https://github.com/settings/tokens> if exposed.

The token is masked (`****abcd`) by the settings API and never returned in
full, but it is stored **in plaintext** in the SQLite database and in `.env`.
Anyone with filesystem access to the data volume can read it.

### Dashboard credentials

- Always prefer `DASHBOARD_PASSWORD_HASH` (salted PBKDF2-HMAC-SHA256, 200,000
  iterations) over the legacy plaintext `DASHBOARD_PASSWORD`. Generate one with
  `python -m app.tools.hash_password`.
- Comparisons are constant-time (`hmac.compare_digest`) to avoid timing oracles.

---

## Deployment hardening

**Always serve the dashboard over HTTPS.** HTTP Basic auth transmits
`base64(user:password)` on every single request. Base64 is an encoding, not
encryption — over plain HTTP your dashboard password is effectively sent in
cleartext to anyone on the network path. Put v2get behind Caddy, Nginx or
Traefik with TLS whenever it is reachable from outside the host.

**Never expose an unauthenticated dashboard.** With all three dashboard
variables blank, authentication is disabled entirely and *anyone* who can reach
the port can read your collected configs, rewrite your settings and read your
GitHub publish target. If you need auth-free access, bind the port to
`127.0.0.1` and reach it through an SSH tunnel:

```yaml
# docker-compose.yml
ports:
  - "127.0.0.1:8080:8080"
```

**Protect the data volume.** `./data` contains the SQLite database (including
the GitHub token), the Telegram-derived output, and the logs. Restrict its
permissions and exclude it from world-readable backups.

**Never commit `.env`.** It is listed in `.gitignore`, but verify with
`git status` before your first push. If a secret is ever committed, rotate it
— removing it from history is not sufficient, because it may already have been
cloned or indexed.

---

## Known design limitations

These are deliberate trade-offs, documented so you can judge the risk:

- **Secrets are stored at rest in plaintext** in `.env` and in the SQLite
  database. There is no envelope encryption or secret-manager integration.
- **HTTP Basic auth only.** No sessions, no rate limiting on login attempts,
  no multi-user support, no audit log of who changed what.
- **No CSRF protection** on the state-changing API endpoints. Basic auth
  credentials are cached by browsers, so a malicious page could in principle
  drive the API in an authenticated browser. Do not browse untrusted sites in a
  session where the dashboard is open, and prefer binding to localhost.
- **TCP liveness checks are unauthenticated outbound connections** to hosts
  advertised in third-party Telegram channels. They confirm a port is open;
  they do not confirm the endpoint is safe, honest or functional as a proxy.
- **Collected proxies are untrusted by definition.** A proxy operator can see
  and modify any traffic you send through it that is not end-to-end encrypted.
  Treat every collected config as run by an unknown party.

## Scope

In scope: authentication bypass, injection, path traversal, SSRF, secret
disclosure, and remote code execution in v2get itself.

Out of scope: vulnerabilities in third-party proxies collected by the tool,
issues requiring pre-existing root access to the host, and the documented
design limitations listed above.
