import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands

from cloner import CloneOptions, ServerCloner

# Guild/channel/role names can contain any Unicode. On Windows the default
# stdout codec is often cp1252, which would crash every print() containing
# such names (UnicodeEncodeError). Force UTF-8 with a safe fallback.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_DEFAULTS = {
    "server_name": True,
    "server_icon": True,
    "server_banner": False,
    "roles": True,
    "categories": True,
    "channels": True,
    "settings": True,
    "permissions": True,
    "emojis": False,
    "stickers": False,
}


# A realistic desktop-Chrome user agent: user tokens are validated against
# Discord's client fingerprinting, and bot/user-agent mismatches can trip it.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)


def die(msg: str, code: int = 1):
    print(f"[X] {msg}", flush=True)
    raise SystemExit(code)


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"config.json is not valid JSON: {e}")
    return data if isinstance(data, dict) else {}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="main.py",
        description="Discord server cloner (bot token ya user token). Emoji/sticker default OFF.",
    )
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument(
        "--token",
        help="Token. If omitted, falls back to the DISCORD_TOKEN env var or config.json.",
    )
    ap.add_argument("--source", help="Source guild ID")
    ap.add_argument("--target", help="Target guild ID")
    ap.add_argument(
        "--token-type",
        choices=["bot", "user"],
        default=None,
        help="bot or user token (default: config.json or 'bot')",
    )

    g = ap.add_argument_group("selection")
    g.add_argument(
        "--interactive",
        action="store_true",
        help="Ask before every step (terminal only).",
    )
    g.add_argument("--pick", action="store_true", help="Select roles/channels by number.")
    g.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only. Nothing will be created.",
    )
    g.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")

    s = ap.add_argument_group("skip")
    s.add_argument("--skip-roles", action="store_true")
    s.add_argument("--skip-categories", action="store_true")
    s.add_argument("--skip-channels", action="store_true")
    s.add_argument("--skip-settings", action="store_true")
    s.add_argument("--skip-permissions", action="store_true")
    s.add_argument("--no-server-name", action="store_true")
    s.add_argument("--no-icon", action="store_true")

    w = ap.add_argument_group("extra")
    w.add_argument("--with-banner", action="store_true")
    w.add_argument("--with-emoji", action="store_true")
    w.add_argument("--with-stickers", action="store_true")
    w.add_argument(
        "--clean-target",
        action="store_true",
        default=True,
        help="Delete existing channels/roles on the target BEFORE cloning (default ON).",
    )
    w.add_argument(
        "--no-clean-target",
        action="store_true",
        help="Keep existing target channels/roles instead of deleting them.",
    )
    return ap.parse_args()


def build_options(args: argparse.Namespace, saved: dict) -> CloneOptions:
    base = dict(DEFAULT_DEFAULTS)
    saved_opts = saved.get("options")
    if isinstance(saved_opts, dict):
        for k, v in saved_opts.items():
            if k in base and isinstance(v, bool):
                base[k] = v

    o = CloneOptions(
        server_name=base["server_name"] and not args.no_server_name,
        server_icon=base["server_icon"] and not args.no_icon,
        server_banner=base["server_banner"] or args.with_banner,
        roles=base["roles"] and not args.skip_roles,
        categories=base["categories"] and not args.skip_categories,
        channels=base["channels"] and not args.skip_channels,
        settings=base["settings"] and not args.skip_settings,
        permissions=base["permissions"] and not args.skip_permissions,
        emojis=base["emojis"] or args.with_emoji,
        stickers=base["stickers"] or args.with_stickers,
        clean_target=(not getattr(args, "no_clean_target", False))
        and bool(saved_opts.get("clean_target", True))
        if isinstance(saved_opts, dict)
        else not getattr(args, "no_clean_target", False),
    )

    tty = sys.stdin.isatty()
    if args.interactive and tty:
        print("\n--- What to copy ---", flush=True)
        o.server_name = ask("Server name?", o.server_name)
        o.server_icon = ask("Server icon?", o.server_icon)
        o.server_banner = ask("Server banner?", o.server_banner)
        o.roles = ask("Roles?", o.roles)
        o.categories = ask("Categories?", o.categories)
        o.channels = ask("Channels?", o.channels)
        o.settings = ask("Channel settings (topic/slowmode/nsfw/bitrate/limit)?", o.settings)
        o.permissions = ask("Permission overwrites?", o.permissions)
        o.emojis = ask("Emojis?", o.emojis)
        o.stickers = ask("Stickers?", o.stickers)
    elif args.interactive:
        print(
            "[!] --interactive needs a TTY on stdin and is unavailable in the dashboard. Skipping.",
            flush=True,
        )
    if args.pick and not tty:
        print(
            "[!] --pick needs a TTY on stdin and is unavailable in the dashboard. Skipping.",
            flush=True,
        )
    return o


def ask(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        v = input(prompt + f" {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not v else v in ("y", "yes")


def pick(items: list, title: str) -> list | None:
    print(f"\n=== {title} ({len(items)}) ===", flush=True)
    for i, it in enumerate(items, 1):
        print(f"  {i:>3}. {it}", flush=True)
    raw = input("Enter numbers (1,2,5-8) | 'all' | 'none' [all]: ").strip().lower()
    if raw in ("", "all", "a"):
        return None
    if raw in ("none", "n", "no", "0"):
        return []
    chosen: list = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                chosen.extend(range(int(a), int(b) + 1))
            else:
                chosen.append(int(part))
        except ValueError:
            pass
    return [items[i - 1] for i in chosen if 1 <= i <= len(items)]


def check_perms(src: discord.Guild, dst: discord.Guild, o: CloneOptions) -> None:
    if src.id == dst.id:
        die(
            "Source and target are the same server. The target must be a different "
            "server (a bot cannot create servers)."
        )
    me = dst.me
    if me is None:
        # User tokens don't populate Guild.me; skip permission pre-check.
        print("[!] permission pre-check skipped (user token or member not cached)", flush=True)
        return
    g = me.guild_permissions
    warns = []
    if o.roles and not g.manage_roles:
        warns.append("the bot lacks 'Manage Roles' on the target, so roles will be skipped")
    if (o.channels or o.categories) and not g.manage_channels:
        warns.append("the bot lacks 'Manage Channels' on the target, so channels will be skipped")
    if (o.server_name or o.server_icon or o.server_banner) and not g.manage_guild:
        warns.append("the bot lacks 'Manage Server' on the target, so name/icon/banner will be skipped")
    if (o.emojis or o.stickers) and not g.manage_expressions:
        warns.append(
            "the bot lacks 'Manage Emojis and Stickers' on the target, so emojis/stickers will be skipped"
        )
    for w in warns:
        print(f"[!] {w}", flush=True)


def build_auth_stripping_trace() -> "aiohttp.TraceConfig":
    """discord.py always sends ``Authorization: Bot <token>`` and a DiscordBot
    user agent for REST calls. User tokens must NOT carry the ``Bot `` prefix,
    and Discord fingerprint-checks user agents too, so rewrite both at the
    aiohttp layer via the official http_trace hook. Mutating the headers object
    here is safe: aiohttp builds the real request from this exact object
    afterwards."""
    trace = aiohttp.TraceConfig()

    async def _rewrite_client_headers(session, ctx, params):
        auth = params.headers.get("Authorization")
        if auth and auth.startswith("Bot "):
            params.headers["Authorization"] = auth[4:]
        if params.headers.get("User-Agent", "").startswith("DiscordBot"):
            params.headers["User-Agent"] = USER_AGENT

    trace.on_request_start.append(_rewrite_client_headers)
    return trace


def _patch_user_login(bot: "discord.Client") -> None:
    """discord.py's Client.login() calls application_info() right after the
    token check, and GET /oauth2/applications/@me is a bot-only endpoint:
    with a user token Discord rejects it with 403 error code 20002 ('Only bots
    can use this endpoint'). Replace login() for user sessions so it stops
    after static_login (/users/@me), which is the only call needed here."""
    import types

    async def login_for_user(self: discord.Client, tok: str) -> None:
        from discord import client as client_mod

        if self.loop is client_mod._loop:
            await self._async_setup_hook()

        data = await self.http.static_login(tok.strip())
        self._connection.user = discord.ClientUser(state=self._connection, data=data)
        # Deliberately no application_info() call here - bot-only endpoint.
        await self.setup_hook()

    bot.login = types.MethodType(login_for_user, bot)  # type: ignore[method-assign]


def _gateway_user_token_patch() -> None:
    """User tokens identify over the gateway WITHOUT the ``Bot `` prefix, and
    Discord expects a real client profile in the IDENTIFY payload. The
    bot-style properties discord.py sends can leave the session in limbo:
    READY never arrives, no error is raised, and the process hangs forever
    (this looked like a crash). Replace identify() entirely so the payload
    matches what the actual Discord client sends."""
    import discord.gateway as gateway_mod

    _build_cache: dict = {"value": None, "fetched_at": 0.0}

    def _probe_build_number() -> int | None:
        """Best-effort scrape of the current stable client build number, using
        the same two-step method self-bot libraries use: grab the login page,
        find the current sentry asset, and read the build number out of it."""
        import re
        import urllib.request

        def _get(url: str) -> str:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed https URL
                return resp.read(4_000_000).decode("utf-8", "replace")

        try:
            login_page = _get("https://discord.com/login")
            match = re.search(r"assets/(sentry\.\w+)\.js", login_page)
            if not match:
                return None
            sentry_js = _get(f"https://static.discord.com/assets/{match.group(1)}.js")
            build_match = re.search(r'buildNumber\D+(\d+)"', sentry_js)
            return int(build_match.group(1)) if build_match else None
        except Exception:
            return None

    async def identify_for_user_token(self):
        if _build_cache["value"] is None or time.time() - _build_cache["fetched_at"] > 3600:
            loop = asyncio.get_running_loop()
            build = await loop.run_in_executor(None, _probe_build_number)
            if build:
                _build_cache["value"] = build
                _build_cache["fetched_at"] = time.time()
            else:
                build = 9999  # stable fallback when the probe fails (same as selfcord)
        else:
            build = _build_cache["value"]

        token = self.token
        if token and token.startswith("Bot "):
            self.token = token[4:]

        payload = {
            "op": self.IDENTIFY,
            "d": {
                "token": self.token,
                "properties": {
                    "os": "Windows",
                    "browser": "Chrome",
                    "device": "",
                    "system_locale": "en-US",
                    "browser_user_agent": USER_AGENT,
                    "browser_version": "144.0.0.0",
                    "os_version": "10",
                    "referrer": "",
                    "referring_domain": "",
                    "referrer_current": "",
                    "referring_domain_current": "",
                    "release_channel": "stable",
                    "client_build_number": build,
                    "client_event_source": None,
                },
                "compress": True,
                "large_threshold": 250,
                "capabilities": (
                    (1 << 0) | (1 << 1) | (1 << 3) | (1 << 12) | (1 << 14) | (1 << 18)
                ),
                "client_state": {"guild_versions": {}},
            },
        }

        state = self._connection
        if state is not None and state._intents is not None:
            payload["d"]["intents"] = state._intents.value

        await self.call_hooks("before_identify", self.shard_id, initial=self._initial_identify)
        await self.send_as_json(payload)
        self._initial_identify = True

    gateway_mod.DiscordWebSocket.identify = identify_for_user_token  # type: ignore[method-assign]


def _hydrated(guild: "discord.Guild | None") -> bool:
    if guild is None:
        return False
    return bool(guild._roles and guild._channels)


async def _wait_for_guild_hydration(bot: discord.Client, guild_id: int, timeout: float = 20.0):
    """With a user token, READY only carries guild skeletons; roles/channels
    arrive per-guild via GUILD_CREATE right after. Wait for the guild to
    hydrate (fall back to a REST fetch if the event never lands)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        guild = bot.get_guild(guild_id)
        if _hydrated(guild):
            return guild
        await asyncio.sleep(0.25)

    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    try:
        data = await bot.http.get_guild(guild_id)
        guild._from_data(data)
    except discord.HTTPException as e:
        print(
            f"[!] gateway did not deliver guild {guild_id} in time and the REST "
            f"fallback failed ({e})",
            flush=True,
        )
    return guild


async def execute_clone(
    token: str,
    source_id: int,
    target_id: int,
    options: CloneOptions,
    dry_run: bool = False,
    allow_pick: bool = False,
    require_confirmation: bool = False,
    token_type: str = "bot",
) -> None:
    token_type = (token_type or "bot").strip().lower()
    if token_type not in ("bot", "user"):
        die(f"Invalid token_type '{token_type}'. Use 'bot' or 'user'.")

    # Bot tokens need the "Bot " prefix; user tokens must NOT have it.
    if token_type == "bot" and not token.startswith("Bot "):
        token = f"Bot {token}"

    intents = discord.Intents.none()
    intents.guilds = True
    intents.emojis_and_stickers = True  # needed for Guild.emojis/stickers to cache

    if token_type == "user":
        # user token: plain Client (no command framework needed) + REST auth fix
        bot = discord.Client(intents=intents, http_trace=build_auth_stripping_trace())
        _patch_user_login(bot)
    else:
        bot = commands.Bot(command_prefix="!", intents=intents)

    print(f"[+] token_type = {token_type}", flush=True)
    print(f"[+] token length = {len(token)}", flush=True)
    state = {"done": False, "ok": False}
    loop = asyncio.get_running_loop()

    def start_watchman(label: str, timeout: float = 90.0):
        """Guarantee the run can never hang forever: if nothing happens within
        the timeout, terminate the process with a clear, honest message.
        Cancelled as soon as real progress is reported."""
        watchdog = loop.call_later(
            timeout,
            lambda: (
                print(
                    f"\n[X] {label} timed out after {timeout:.0f}s. "
                    "Discord never completed the connection (check your token type and network). "
                    "Stopping instead of hanging.",
                    flush=True,
                ),
                os._exit(1),
            ),
        )
        return watchdog

    watch = {"active": start_watchman("Gateway connection")}

    @bot.event
    async def on_ready():
        if state["done"]:
            return
        state["done"] = True
        watch["active"].cancel()
        try:
            if token_type == "user":
                # With a user token, READY only carries guild skeletons; the
                # full guild payloads arrive via GUILD_CREATE moments later.
                print("[i] waiting for guild data from the gateway...", flush=True)
                src, dst = await asyncio.gather(
                    _wait_for_guild_hydration(bot, source_id),
                    _wait_for_guild_hydration(bot, target_id),
                )
            else:
                src = bot.get_guild(source_id)
                dst = bot.get_guild(target_id)
            if src is None:
                die(
                    f"Source guild {source_id} is not accessible with this token. "
                    "Check the ID, or make sure the bot/account is a member of that server."
                )
            if dst is None:
                die(
                    f"Target guild {target_id} is not accessible with this token. "
                    "Check the ID, or make sure the bot/account is a member of that server."
                )

            print(f"[+] connected as {bot.user} (id {bot.user.id})", flush=True)
            # Cloning can legitimately take a while on big servers, so re-arm
            # the watchdog with a generous ceiling for the clone itself.
            watch["active"] = start_watchman("Clone run", timeout=1800.0)
            print(
                f"[+] source: {src.name} ({src.id}) | {len(src.channels)} channels, {len(src.roles)} roles",
                flush=True,
            )
            print(
                f"[+] target: {dst.name} ({dst.id}) | {len(dst.channels)} channels, {len(dst.roles)} roles",
                flush=True,
            )
            print(f"[+] options: {options.dump()}", flush=True)

            check_perms(src, dst, options)

            if allow_pick:
                if options.roles:
                    options.only_roles = pick([r.name for r in src.roles if not r.is_default()], "ROLES")
                if options.channels or options.categories:
                    options.only_channels = pick([c.name for c in src.channels], "CHANNELS + CATEGORIES")
                if options.emojis:
                    options.only_emojis = pick([e.name for e in src.emojis if e.name], "EMOJIS")

            if options.only_roles is not None:
                print(f"[+] only roles: {options.only_roles}", flush=True)
            if options.only_channels is not None:
                print(f"[+] only channels: {options.only_channels}", flush=True)

            if dry_run:
                print("\n--- DRY RUN: nothing will be created ---", flush=True)
                print(f"roles    : {len([r for r in src.roles if not r.is_default()])}", flush=True)
                print(f"channels : {len(list(src.channels))}", flush=True)
                print(f"emojis   : {len([e for e in src.emojis if e.name]) if options.emojis else 0}", flush=True)
                print(f"stickers : {len(list(src.stickers)) if options.stickers else 0}", flush=True)
                print(f"target currently has: {[c.name for c in dst.channels]}", flush=True)
                return

            if require_confirmation and not ask(
                f"\n[?] Start cloning into {dst.name}? "
                + (
                    "Existing channels/roles on the target WILL BE DELETED first!"
                    if options.clean_target
                    else "(Existing channels in the target will NOT be deleted.)"
                ),
                default=False,
            ):
                print("Cancelled.", flush=True)
                return

            cloner = ServerCloner(src, dst, options)
            report = await cloner.run()
            print(report.dump(), flush=True)
            if report.notes:
                print("\nnotes:", flush=True)
                for n in report.notes:
                    print(f"  - {n}", flush=True)
            if cloner.missing_overwrite_roles:
                print(
                    "\n[!] channel permission overwrites skipped for roles that could not be cloned:",
                    flush=True,
                )
                for name in sorted(cloner.missing_overwrite_roles):
                    print(f"  - {name}", flush=True)
            print(f"\n[+] done -> https://discord.com/channels/{dst.id}/@me", flush=True)
            print(
                f"\n[+] Source server '{getattr(src, 'name', 'unknown')}' cloned in "
                f"target server '{getattr(dst, 'name', 'unknown')}' "
                "by QUANTUM DEVS Discord Server Cloner",
                flush=True,
            )
            state["ok"] = True
        except SystemExit:
            raise
        except discord.LoginFailure:
            state["ok"] = False
            die(
                "Invalid token (LoginFailure). Copy a fresh token from the Developer Portal "
                "(bot) or your account (user)."
            )
        except discord.Forbidden as e:
            state["ok"] = False
            die(f"Forbidden: {e}. The bot/account is missing a permission or access.")
        except discord.ClientException as e:
            state["ok"] = False
            die(str(e))
        except Exception as e:
            state["ok"] = False
            print(f"[X] {type(e).__name__}: {e}", flush=True)
            raise SystemExit(1) from None
        finally:
            await bot.close()

    if token_type == "user":
        # Gateway IDENTIFY must not carry the "Bot " prefix for user tokens.
        _gateway_user_token_patch()

    try:
        await bot.start(token)
    except discord.LoginFailure:
        die(
            "Invalid token (LoginFailure). Copy a fresh token from the Developer Portal "
            "(bot) or your account (user)."
        )
    except discord.HTTPException as e:
        die(f"Could not connect to Discord: {e}")
    finally:
        http = getattr(bot, "http", None)
        if http is not None:
            with contextlib.suppress(Exception):
                await http.close()

    # bot.start() returning normally is NOT success: on_ready can have failed
    # internally (discord.py swallows task exceptions). Report the real outcome.
    if not state.get("ok", False):
        raise SystemExit(1)


async def main() -> None:
    args = parse_args()
    saved = load_config(Path(args.config))

    # token_type priority: CLI > env > config > default "bot"
    token_type = (
        args.token_type
        or os.environ.get("DISCORD_TOKEN_TYPE")
        or saved.get("token_type")
        or "bot"
    )
    token_type = str(token_type).strip().lower()
    if token_type not in ("bot", "user"):
        die(f"token_type '{token_type}' is invalid. It must be 'bot' or 'user'.")

    token = os.environ.get("DISCORD_TOKEN") or args.token or saved.get("token") or ""
    token = str(token).strip().strip('"').strip("'")
    if not token or token in ("YOUR_DISCORD_BOT_TOKEN", "YOUR_TOKEN"):
        die("token missing. Provide it via --token, the DISCORD_TOKEN env var, or config.json.")

    # strip "Bot " prefix if present (we re-add in execute_clone based on type)
    if token.startswith("Bot "):
        token = token[4:].strip()

    source_raw = args.source or saved.get("source_guild_id")
    target_raw = args.target or saved.get("target_guild_id")
    if not source_raw or not target_raw:
        die("Both source and target guild IDs are required (--source/--target or config.json).")
    try:
        source_id, target_id = int(str(source_raw).strip()), int(str(target_raw).strip())
    except ValueError:
        die(
            "Guild IDs must be numeric. Enable Developer Mode in the Developer Portal, "
            "then copy the ID."
        )

    options = build_options(args, saved)

    await execute_clone(
        token,
        source_id,
        target_id,
        options,
        dry_run=args.dry_run,
        allow_pick=args.pick and sys.stdin.isatty(),
        require_confirmation=not args.yes,
        token_type=token_type,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted.", flush=True)
        raise SystemExit(130) from None
    except SystemExit:
        raise
