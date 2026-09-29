# Discord Server Cloner

Copies the full structure of a server (roles, categories, channels, settings, permissions) into
another server. Runs with a **bot token** or a **user token**.

> ⚠️ **User tokens (self-bots) violate Discord's Terms of Service** and can get the account
> permanently banned. They are supported here because the tool is often used on the user's own
> servers — use at your own risk. A bot token is always the safer option.

## Token options

### Bot token (recommended)

1. <https://discord.com/developers/applications> → **New Application** → create a `Bot`.
2. **Bot → Reset Token** → copy the token.
3. In the OAuth URL, select the `bot` scope and tick these permissions, then invite the bot to
   **both** of your servers:
   `Manage Roles`, `Manage Channels`, `Manage Server`, `Manage Emojis and Stickers`.
   Use **Bot → URL Generator** to build the invite link.

### User token (self-bot, ban risk)

1. Log in to your account in the Discord desktop app.
2. Press `Ctrl+Shift+I` → **Network** tab → `F5` (reload).
3. Click the first request → **Request Headers** → copy the `authorization` value.
   This value does NOT have a `Bot ` prefix — paste it exactly as it is.
4. Select **User Token** in the token type dropdown, then paste the token.
5. Your account must be a member of both servers and have **Manage Roles / Manage
   Channels / Manage Server / Manage Emojis** in the target (server owner = perfect).

> When a user account edits a server, Discord sometimes requires an MFA step-up.
> If the target edit fails with `Forbidden`, send any message in the target server from
> your account once, then run the clone again.

## Common setup (both token types)

1. `discord.com` → **User Settings → Advanced → Developer Mode ON**.
2. Right-click the server name → **Copy ID** (for both the source and the target).

> Bots cannot *create* servers (user accounts cannot either under normal rate limits),
> so the target server must already exist.
> An empty server works best. Existing channels in the target are never deleted.

## Dashboard (recommended)

```
run_dashboard.bat
```

It opens a desktop window (falling back to `http://127.0.0.1:5000` in a browser). It binds to
localhost only.

## CLI

```
run_cli.bat --dry-run
run_cli.bat
run_cli.bat --skip-roles --with-emoji
```

The token can come from `config.json` or the `DISCORD_TOKEN` environment variable.
Priority: `--token` > env var > config.json.

Token type: `--token-type user` or `--token-type bot` (default `bot`; you can also set the
`DISCORD_TOKEN_TYPE` env var or `token_type` in config.json). A `Bot ` prefix is added to bot
tokens automatically.

### Flags

| Flag | Description |
| --- | --- |
| `--source` / `--target` | Guild IDs |
| `--token-type` | `bot` or `user` (default: config.json or `bot`) |
| `--dry-run` | Preview only. Nothing is created. |
| `--interactive` | Ask before every step (terminal only) |
| `--pick` | Select roles/channels by number |
| `--skip-roles` `--skip-categories` `--skip-channels` | Skip structure |
| `--skip-settings` `--skip-permissions` | Skip details |
| `--no-server-name` `--no-icon` `--with-banner` | Server metadata |
| `--with-emoji` `--with-stickers` | Emojis and stickers (OFF by default) |
| `--clean-target` | Delete existing channels/roles on the target BEFORE cloning (ON by default) |
| `--no-clean-target` | Keep existing target channels/roles instead |

Emojis and stickers require free slots on the bot plus `Manage Emojis and Stickers`.
LOTTIE stickers cannot be cloned (a note is added to the report).

## What gets cloned

- Server name, icon, and (optionally) the banner
- Roles — colour, permissions, hoist, mentionable (managed/integration roles are skipped)
- Categories and channels — text, announcement, voice, stage, forum, media
- Channel settings — topic, slowmode, NSFW, bitrate, user limit
- Position and order
- Permission overwrites (@everyone + per role; member-specific overwrites are skipped because
  they are tied to individual user IDs)
- Emojis and stickers (opt-in)

## Files

- `cloner.py` — clone engine (discord.py)
- `main.py` — CLI entrypoint
- `web_app.py` — Flask dashboard and job runner
- `templates/index.html`, `static/` — frontend
- `test_cloner.py` — offline engine test (runs without a real Discord connection)
- `config.json` — token, guild IDs, and last used options (git-ignored)

## Test

```
.venv\Scripts\python.exe test_cloner.py
```

Verifies that the kwargs for every channel type match discord.py's real signatures, checks
overwrite mapping, and confirms that `--skip-settings` / `--skip-permissions` disable everything.

## Notes

- Members, messages, bans, webhooks, and threads are not cloned.
- If the source already contains duplicate roles or channels, the existing ones are reused
  (nothing is deleted).
- Logs appear in `logs/` and in the dashboard's "Live output" panel.
- The dashboard binds only to `127.0.0.1`, the token stays masked in the browser DOM, and it is
  passed to the child process through an environment variable (`DISCORD_TOKEN`), never on the
  command line.
- **Clean target** (default ON) deletes every cloneable channel and unmanaged role on the target
  before cloning. @everyone and managed/integration roles are always kept. Pass `--no-clean-target`
  (or untick the dashboard checkbox) to keep what is already there.
