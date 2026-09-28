# Discord Server Cloner

Ek server ka pura structure (roles, categories, channels, settings, permissions) dusre server me
copy karta hai. **Bot token** se chalta hai.

> ⚠️ **User token (self-bot) supported nahi hai.** Wo Discord ToS violation hai aur account
> turant permanently ban ho jata hai. Sirf bot token use karo.

## Setup (ek baar)

1. <https://discord.com/developers/applications> → **New Application** → `Bot` banao.
2. **Bot → Reset Token** → token copy karo.
3. OAuth URL me `bot` scope aur ye permissions tick karo, phir apne **dono** servers me invite karo:
   `Manage Roles`, `Manage Channels`, `Manage Server`, `Manage Emojis and Stickers`.
   Invite link banane ke liye **Bot → URL Generator** use karo.
4. `discord.com` → **User Settings → Advanced → Developer Mode ON**.
5. Server name par right-click → **Copy ID** (source aur target dono ke liye).

> Discord bots servers *create* nahi kar sakte, isliye target server pehle se bana hua hona chahiye.
> Empty server best rehta hai. Target ke existing channels kabhi delete nahi hote.

## Dashboard (recommended)

```
run_dashboard.bat
```

Browser me khul jayega `http://127.0.0.1:5000`. Sirf localhost pe bind hota hai.

## CLI

```
run_cli.bat --dry-run
run_cli.bat
run_cli.bat --skip-roles --with-emoji
```

Token `config.json` me ya `DISCORD_TOKEN` env var me. Priority: `--token` > env var > config.json.

### Flags

| Flag | Kaam |
| --- | --- |
| `--source` / `--target` | guild IDs |
| `--dry-run` | sirf preview, kuch create nahi |
| `--interactive` | har step pe poochhe (terminal me) |
| `--pick` | roles/channels numbers se choose karo |
| `--skip-roles` `--skip-categories` `--skip-channels` | structure skip |
| `--skip-settings` `--skip-permissions` | details skip |
| `--no-server-name` `--no-icon` `--with-banner` | server meta |
| `--with-emoji` `--with-stickers` | emojis/stickers (default OFF) |

Emoji/sticker ke liye bot ke paas free slots aur `Manage Emojis and Stickers` chahiye.
LOTTIE stickers clone nahi hote (report me note aa jayega).

## Kya clone hota hai

- server name, icon, (optionally) banner
- roles — color, permissions, hoist, mentionable (managed/integration roles skip)
- categories aur channels — text, announcement, voice, stage, forum, media
- channel settings — topic, slowmode, NSFW, bitrate, user limit
- position/order
- permission overwrites (@everyone + role-wise; member-specific overwrites skip hote hain kyunki
  woh logon ke IDs se tied hote hain)
- emojis aur stickers (opt-in)

## Files

- `cloner.py` — clone engine (discord.py)
- `main.py` — CLI entrypoint
- `web_app.py` — Flask dashboard + job runner
- `templates/index.html`, `static/` — frontend
- `test_cloner.py` — offline engine test (real Discord ke bina chalata hai)
- `config.json` — token + guild IDs + last used options (git-ignored)

## Test

```
.venv\Scripts\python.exe test_cloner.py
```

Har channel type ke liye check karta hai ki discord.py ke actual signature se kwargs match hain,
overwrite mapping, aur ki `--skip-settings` / `--skip-permissions` sab kuch off karte hain.

## Notes

- Members, messages, bans, webhooks, threads clone nahi hote.
- Ek hi source me duplicate roles/channels pehle se maujood ho to wo reuse hote hain (delete nahi).
- Log `logs/` me aur dashboard ke "Live output" me dikhte hain.
- Dashboard sirf `127.0.0.1` pe bind hota hai, token browser DOM me masked rehta hai aur child
  process ko env var (`DISCORD_TOKEN`) se jaata hai, command line se nahi.
