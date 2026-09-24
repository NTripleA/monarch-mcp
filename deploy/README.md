# Remote deployment: Raspberry Pi behind Cloudflare Access

This runs monarch-mcp as a remote MCP server that both Claude and ChatGPT reach through a single connector. Replace `monarch.example.com` and `mcp-pi` below with your own hostname and SSH/tunnel target in your private deployment configuration:

```
Claude / ChatGPT connector
  └─ https://monarch.example.com/mcp
       └─ Cloudflare Access (Managed OAuth; "Protect with Access")
            └─ Cloudflare Tunnel "mcp-pi"
                 └─ Raspberry Pi: HTTP 127.0.0.1:8004   (host loopback only)
                      └─ Docker container "monarch-mcp": 0.0.0.0:8000/mcp  (uid 10001, read-only rootfs)
                           └─ /state/session.pickle     (bind mount of /opt/mcp/monarch/state)
```

Cloudflare Access is the only authentication layer; there is no second bearer token. The container port is published on the Pi's loopback interface only, so the tunnel is the only way in.

## Security model

| Concern | How it is handled |
|---|---|
| Who can connect | Cloudflare Access policy. Nothing listens on a public interface. |
| DNS rebinding / Host spoofing | `/mcp` accepts only loopback hosts plus `MONARCH_ALLOWED_HOSTS`. Any other Host header gets 421, and a foreign browser Origin gets 403. |
| Accidental writes | `MONARCH_ENABLE_WRITES` defaults to `false` in HTTP mode. Write tools are left out of `tools/list` **and** rejected if a client with a cached tool list calls them anyway. |
| Credentials on the Pi | The provisioned session file contains reusable credentials. Keep it private (directory 0700, file 0600), exclude it from Git and shared backups, and never paste it into logs or support requests. The server loads the session you provision on your Mac. In HTTP mode it ignores password, MFA, token, and cookie environment variables and removes them from its process environment. It never attempts a login. |
| Expired session | Tool calls fail with a message to reprovision. The session file is **not** deleted, and there are no login retries. `/healthz` stays up. When a new `session.pickle` is copied in, the server picks it up automatically without a restart. |
| Browser sign-in tool | `authenticate_browser_session` is stdio-only. Over HTTP it is never listed and cannot be called. |
| Tampered session file | Loaded with a restricted unpickler that cannot construct objects. Symlinks, group- or world-writable files, and unexpected fields are refused. |
| Logs | Tool name, outcome, duration, result size and count, and error *type/category* only. No arguments, IDs, merchants, amounts, notes, search text, payloads, or credentials. Library tracebacks are reduced to the exception type. No access log. |
| Request size | `/mcp` bodies over 1 MiB get 413 (`MONARCH_HTTP_MAX_BODY_BYTES`). |

## 1. Provision the session on your Mac

The Pi never signs in by itself. You create the session on the Mac and copy it over.

```bash
cd /path/to/monarch-mcp
uv sync

# Opens a local page. Sign in at app.monarch.com, then click "Detect my session"
# or paste the full Cookie header from a GraphQL request in DevTools.
uv run python server.py --provision-session --output-dir ~/.monarch-mcp-pi
```

If you have a Monarch API token (the value after `Token ` in the `Authorization` header of an app.monarch.com GraphQL request), a token session is more portable than a cookie session. Read it without echoing it to the screen or your shell history:

```bash
read -rs MONARCH_TOKEN && export MONARCH_TOKEN
uv run python server.py --provision-session --output-dir ~/.monarch-mcp-pi
unset MONARCH_TOKEN
```

This writes `~/.monarch-mcp-pi/session.pickle` with mode 0600 and prints only the number of accounts it could see.

> Don't click "Log out" in the browser session you provisioned from. Logging out invalidates the server-side session the Pi is using. Closing the tab or window is fine.

### Copy it to the Pi

```bash
PI=user@mcp-pi          # your SSH target

# Lands in your Pi home directory as 0600 (a plain scp could leave it world-readable).
ssh "$PI" 'umask 077 && cat > ~/monarch-session.pickle' < ~/.monarch-mcp-pi/session.pickle
ssh -t "$PI" 'sudo install -o 10001 -g 10001 -m 0600 ~/monarch-session.pickle /opt/mcp/monarch/state/session.pickle.new \
  && sudo mv /opt/mcp/monarch/state/session.pickle.new /opt/mcp/monarch/state/session.pickle \
  && rm -f ~/monarch-session.pickle'

rm -f ~/.monarch-mcp-pi/session.pickle   # remove this temporary local copy after verifying the transfer
```

The `install` + `mv` swaps the file in atomically inside the state directory. Because the server detects the change, it needs no restart.

## 2. One-time Pi setup

```bash
# Directories: state is private to the container user (10001).
sudo install -d -m 0755 /opt/mcp/monarch
sudo install -d -o 10001 -g 10001 -m 0700 /opt/mcp/monarch/state

# Code: clone your fork (or rsync it from the Mac, excluding .git, .venv and .env*).
sudo git clone https://github.com/NTripleA/monarch-mcp.git /opt/mcp/monarch/src

# Settings (no credentials in here). Mode 0600, owned by whoever runs `docker compose`.
sudo install -m 0600 /opt/mcp/monarch/src/deploy/monarch-mcp.env.example /opt/mcp/monarch/monarch-mcp.env
sudoedit /opt/mcp/monarch/monarch-mcp.env    # check MONARCH_ALLOWED_HOSTS=monarch.example.com

# Build (native arm64) and start.
cd /opt/mcp/monarch/src
sudo docker compose up -d --build

# Then provision the session (section 1) and verify (section 3).
```

## 3. Verify on the Pi

```bash
curl -s http://127.0.0.1:8004/healthz                # {"status":"ok"}
sudo docker inspect -f '{{.State.Health.Status}}' monarch-mcp   # healthy

# MCP over loopback (Host 127.0.0.1:8004 is always allowed):
mcp() { curl -s http://127.0.0.1:8004/mcp -H 'Content-Type: application/json' \
          -H 'Accept: application/json, text/event-stream' -d "$1" | sed -n 's/^data: //p'; }
mcp '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -c 'import json,sys; print(sorted(t["name"] for t in json.load(sys.stdin)["result"]["tools"]))'
# With writes off: 15 read-only tools, including monarch_auth_status.

# Session health. verify=true makes one lightweight Monarch request and discards the response.
mcp '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"monarch_auth_status","arguments":{"verify":true}}}'

# A foreign Host header must be refused:
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8004/mcp -H 'Host: evil.example' \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/list"}'     # 421

sudo docker compose logs --tail 50 monarch-mcp
```

## 4. Cloudflare (configured in the dashboard, not in this repo)

1. **Tunnel.** On the existing `mcp-pi` tunnel, add a public hostname `monarch.example.com` with service **HTTP** → `127.0.0.1:8004`.
   - Leave the origin Host header as the public hostname (the default). If you override it, add the override to `MONARCH_ALLOWED_HOSTS`, or every request gets 421.
   - If `cloudflared` itself runs in a container, it needs host networking for `127.0.0.1:8004` to reach the Pi's loopback.
2. **Access.** Create an Access application for `monarch.example.com` and turn on **Protect with Access**. Enable **Managed OAuth** so Claude and ChatGPT can complete the OAuth flow. Restrict the policy to your own identity.
3. **Connector URL.** Add `https://monarch.example.com/mcp` as a custom connector in Claude and in ChatGPT.
4. **Claude root-path workaround** (only if needed). If Claude's hosted connector sends requests to `/` instead of `/mcp`, add a Cloudflare URL-rewrite rule for this hostname that rewrites path `/` → `/mcp`. It must be an internal rewrite, not a redirect. The server only serves `/mcp` and `/healthz`.

`/mcp` answers `POST` only. The server is stateless, so `GET` (a server-push stream) and `DELETE` (session teardown) return 405, which the MCP spec allows.

## 5. Day-2 operations

**Refresh an expired session.** Repeat section 1 (provision + copy). No restart is needed. Check with `monarch_auth_status` (`verify: true`).

**Enable writes** (or change any setting in the env file):

```bash
sudoedit /opt/mcp/monarch/monarch-mcp.env         # MONARCH_ENABLE_WRITES=true
cd /opt/mcp/monarch/src && sudo docker compose up -d --force-recreate monarch-mcp
```

The env file is read only when the container is **created**. `docker restart` and `docker compose restart` keep the old environment, so a changed setting silently does not apply.

**Update the code:**

```bash
cd /opt/mcp/monarch/src && sudo git pull && sudo docker compose up -d --build
```

### Write-tool reference

| Tool | Effect | Annotation |
|---|---|---|
| `create_transaction` | Adds a transaction | write, not destructive |
| `create_manual_account` | Adds a manual account | write, not destructive |
| `update_transaction` | Overwrites fields on a transaction | destructive, idempotent |
| `update_transactions_bulk` | Same, up to `MONARCH_MAX_BULK_UPDATES` (default 25) per call; the whole batch is validated first | destructive, idempotent |
| `update_transaction_splits` | Replaces the full split set (an empty list removes all splits) | destructive, idempotent |
| `set_budget_amount` | Replaces a category's budget amount | destructive, idempotent |
| `refresh_accounts` | Asks Monarch to re-sync all institutions | write, idempotent |

Writes are never retried after an ambiguous failure (timeout, dropped connection, 5xx). Instead, the error says the change *may* have been applied. For creates, it tells the client to search for the record before retrying.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Every tool: "No Monarch session is provisioned" | `/opt/mcp/monarch/state/session.pickle` is missing. Provision it (section 1). |
| "Monarch rejected the saved session" | The session expired or was logged out. Reprovision; no restart needed. |
| "session file is writable by other users" | Run `sudo chmod 600 /opt/mcp/monarch/state/session.pickle`. |
| "Permission denied" reading the session | Run `sudo chown 10001:10001 /opt/mcp/monarch/state/session.pickle` (and the directory). |
| 421 from the public URL | The tunnel forwards a Host that isn't in `MONARCH_ALLOWED_HOSTS`. |
| Write tools missing | `MONARCH_ENABLE_WRITES` is false, or the env file changed without `--force-recreate`. |
| `authenticate_browser_session` missing | Expected: it only exists over local stdio. |
