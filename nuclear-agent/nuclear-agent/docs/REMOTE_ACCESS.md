# Remote access – design notes (not built yet)

Nuclear Agent can drive a real browser that may be logged in to your accounts.
Anyone who can send it tasks can act as you on the web. So today it is
**local-only**, and any future remote access must be added deliberately and with
authentication.

> Putting this repository on GitHub does **not** give ChatGPT or anyone else
> access to your computer. GitHub only stores the code. Something on your PC
> must be running *and* reachable *and* must accept the caller's credentials
> before any remote client could submit a task. None of that exists yet.

## What protects the agent today

| Protection | Where |
|---|---|
| Server binds to `127.0.0.1` only. A non-loopback host is refused at startup. | `nuclear_agent/__main__.py` |
| `server.host` cannot be changed from the control panel. | `nuclear_agent/config.py` (`Config.update`) |
| Every `/api/*` call needs the `X-Nuclear-Token` header. The token is random on each start and only embedded in the panel page this process serves. | `nuclear_agent/server.py` |
| The `Host` header must be a loopback name. This blocks DNS-rebinding attacks from websites. | `server.py` middleware |
| Requests with a foreign `Origin` are rejected, so other websites can't trigger tasks from your browser. | `server.py` middleware |
| No CORS headers are sent, so other sites can't read responses. | default FastAPI behaviour |
| Sensitive actions wait for approval in the panel. Passwords, payment data and CAPTCHAs are user-only. | `nuclear_agent/safety.py`, `agent.py` |

## How a secure remote bridge could be added later

### 1. Keep the core local and add a separate bridge

```
 Remote client (e.g. ChatGPT action / MCP client / phone)
        │  HTTPS + per-client API key or OAuth
        ▼
 ┌─────────────────────────┐   private network or tunnel
 │  bridge (new component) │   (Tailscale, WireGuard, Cloudflare Tunnel + Access)
 └───────────┬─────────────┘
             │  calls the same internal API on 127.0.0.1 with the local token
             ▼
      Nuclear Agent core (unchanged, still localhost-only)
```

* The bridge should be its own process with its own small API. The core keeps
  listening on localhost only.
* **Prefer a private network** (Tailscale/WireGuard) over opening a port on your
  router. If it must be public, put it behind a tunnel with an identity check
  (for example Cloudflare Access) **and** API keys.
* Only HTTPS. Never plain HTTP over the internet.

### 2. Authentication and permissions

* Long random API keys per client (`secrets.token_urlsafe(32)`), stored **hashed**
  (e.g. SHA-256) in `data/`, never in the repo. Each key can be revoked on its own.
* Scopes per key, for example:
  * `tasks:submit` – start a task (goal + optional start URL)
  * `tasks:read` – read status, history and results
  * no remote scope for manual browser control, settings or the profile
* Rate limiting (e.g. 10 tasks/hour per key) and an audit log of every remote call.
* Optional allow-list of domains a remote task may visit.

### 3. Approvals stay local

The most important rule: **a remote client must never be able to approve its own
sensitive action.** Approvals (purchases, messages, account changes, personal
data) should still need someone at the PC, or a second channel the remote
client doesn't control (e.g. a push notification to your phone with its own
login).

### 4. Suggested bridge API

```
POST /remote/v1/tasks              {goal, start_url?}       -> {task_id}
GET  /remote/v1/tasks/{id}                                   -> {status, step, result, needs_approval}
GET  /remote/v1/tasks/{id}/actions?limit=20                  -> recent actions
POST /remote/v1/tasks/{id}/stop
```

Inside the bridge these map directly onto the existing local endpoints
(`/api/task`, `/api/tasks/{id}`, `/api/tasks/{id}/actions`, `/api/task/stop`).
All task logic lives in `AgentManager` (`nuclear_agent/agent.py`), so the bridge
needs no changes to the agent itself.

### 5. Connecting an AI assistant

Assistants that can call external tools (for example custom GPT "Actions" or
MCP-based connectors) can only reach an HTTPS endpoint you expose and
authorize. Describe the bridge API above in an OpenAPI or MCP tool definition,
give the assistant **one revocable key with only `tasks:submit` + `tasks:read`**,
and keep approvals local.

### Checklist before enabling any remote access

- [ ] Bridge runs as a separate process; core still bound to 127.0.0.1
- [ ] HTTPS only, through a private network or an authenticated tunnel
- [ ] Hashed, revocable, scoped API keys; none in the repository
- [ ] Rate limits and an audit log
- [ ] Sensitive-action approval still needs the local user
- [ ] Tested that requests without a key, with a wrong key, or from an unknown host are rejected
