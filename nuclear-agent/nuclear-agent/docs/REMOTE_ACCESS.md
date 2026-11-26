# AI connections & remote access

Nuclear Agent can be used as a **tool by other AIs**, the same way hosted
services like TinyFish are, through MCP (Model Context Protocol). It can also
be controlled from the **GitHub Pages panel** (on this PC or your phone).

The difference from a hosted service: the browser and the AI model run on
**your** PC, so the PC must be on and Nuclear Agent must be running.

> Putting the repository on GitHub does **not** give anyone access to your PC.
> Access needs: Nuclear Agent running + a valid API key + (from outside the PC)
> remote access switched on and a tunnel running.

---

## 1. Claude Desktop on this PC (easiest, nothing exposed to the internet)

1. Install and start Nuclear Agent once (`install.bat`, `start.bat`).
2. Double-click **`connect_claude.bat`**. It adds Nuclear Agent to Claude Desktop's
   config (a backup of your old config is saved next to it).
3. Fully quit Claude Desktop (tray icon → Quit) and open it again.
4. Ask Claude: *"Use Nuclear Agent to find the opening hours of Systembolaget in Örnsköldsvik."*

Claude starts `nuclear_mcp.py` itself. If Nuclear Agent isn't running, it is
started in the background. The connection uses a local-only key
(`data/local_mcp_key.txt`) that does not work from outside this PC.

**Claude Code:** `claude mcp add nuclear-agent -- "<path>\.venv\Scripts\python.exe" "<path>\nuclear_mcp.py"`
(`connect_claude.bat` prints the exact command).

## 2. Other AI apps on this PC (Cursor, Windsurf, Codex …)

MCP over HTTP: `http://127.0.0.1:8765/mcp` with header
`Authorization: Bearer <AI key>`. Create the key in the panel: **Settings → AI connections → API keys**, type **AI**.

## 3. ChatGPT, your phone, or anything outside this PC (tunnel)

1. In the panel: **Settings → AI connections** → tick **Allow remote access** → **Save**.
2. Create a key of type **AI** (for ChatGPT) or **Panel (you)** (for yourself on the phone).
3. Install Cloudflare's tunnel program once: `winget install --id Cloudflare.cloudflared`
4. Run **`remote.bat`**. It prints an address like `https://random-words.trycloudflare.com`.
   It changes every time you start the tunnel, and stops working when you close the window.
5. Use it:
   * **AI apps that can send headers:** `https://….trycloudflare.com/mcp` + `Authorization: Bearer <key>`
   * **AI apps that only take a URL** (e.g. a ChatGPT custom MCP connector): `https://….trycloudflare.com/mcp/k/<key>`.
     Treat this URL like a password.
   * **GitHub Pages panel on the phone:** enter `https://….trycloudflare.com` as the agent address and a **Panel** key.

## 4. The GitHub Pages panel

`docs/index.html` is a complete control panel in one HTML file. To publish it:
GitHub repo → **Settings → Pages** → *Build and deployment*: **Deploy from a branch** →
branch **main**, folder **/docs** → Save. After a minute it is at
`https://<your-name>.github.io/<repo>/`.

Before it can connect, add `https://<your-name>.github.io` to **Settings → AI connections →
Web pages allowed to connect** (already set in `config.example.json` for this repo), and log in
with a **Panel** key.

* On the same PC use agent address `http://127.0.0.1:8765`. Chrome/Edge may ask to allow
  access to devices on your local network; allow it. Safari may block this; use the tunnel instead.
* From other devices use the tunnel address (step 3).

---

## Security model (what is enforced, and tested)

| Rule | Where |
|---|---|
| Server listens on 127.0.0.1 only; a tunnel forwards to it. | `__main__.py` |
| Every `/remote/*` and `/mcp` call needs a valid API key. Only SHA-256 hashes are stored. | `keys.py`, `remote.py` |
| Requests arriving through a tunnel (Host not localhost) are refused unless *Allow remote access* is on. | `remote.authenticate` |
| The local-only key (Claude Desktop) never works through a tunnel. | `remote.authenticate` |
| The local panel and `/api/*` are **never** reachable through a tunnel, token or not. | `server.guard` |
| Browsers can only call from listed origins (CORS allow-list). Other sites get 403. | `server.guard` |
| Scopes: **AI** keys = read, submit, control. Only **Panel** keys (a person) can *approve*. An AI can never approve its own purchase/message. | `keys.PRESETS`, `service.py` |
| Passwords, payment data and CAPTCHAs remain user-only for every key. | `safety.py` |
| Rate limits per key (requests/minute, task starts/hour). | `keys.py`, config `remote.*` |
| Every remote write (start, stop, reply, approve) is logged to `data/logs/remote_audit.jsonl`. | `service.audit` |
| Keys can be revoked instantly in the panel. | Settings → AI connections |

Tests: `tests/test_remote.py` (scopes, approval rule, tunnel rules, CORS, rate limits, MCP over
HTTP and stdio, Claude Desktop config merge, and the GitHub Pages panel driving the agent from a
different origin in a real browser).

## Not done (ideas for later)

* OAuth login instead of API keys (needed by some hosted AI apps that refuse key-in-URL connectors).
* A fixed tunnel address (named Cloudflare tunnel; needs a free Cloudflare account and a domain).
* Push notification to your phone when an approval is needed.
