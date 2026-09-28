import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import discord
from discord.ext import commands

from cloner import CloneOptions, ServerCloner

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


def die(msg: str, code: int = 1):
    print(f"[X] {msg}", flush=True)
    raise SystemExit(code)


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        die(f"config.json valid JSON nahi hai: {e}")
    return data if isinstance(data, dict) else {}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        prog="main.py",
        description="Discord server cloner (BOT token only). Emoji/sticker default OFF.",
    )
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--token", help="bot token (nahi diya to env DISCORD_TOKEN ya config.json se)")
    ap.add_argument("--source", help="source guild id")
    ap.add_argument("--target", help="target guild id")
    ap.add_argument(
        "--token-type",
        choices=["bot", "user"],
        help="deprecated: sirf 'bot' supported hai (user token = self-bot = ToS violation)",
    )

    g = ap.add_argument_group("selection")
    g.add_argument("--interactive", action="store_true", help="har step pe poochega (terminal me hi)")
    g.add_argument("--pick", action="store_true", help="roles/channels numbers se choose karo")
    g.add_argument("--dry-run", action="store_true", help="sirf preview, kuch create nahi hoga")
    g.add_argument("--yes", action="store_true", help="confirmation mat poochho")

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
    )

    tty = sys.stdin.isatty()
    if args.interactive and tty:
        print("\n--- kya kya copy karna hai ---", flush=True)
        o.server_name = ask("server name?", o.server_name)
        o.server_icon = ask("server icon?", o.server_icon)
        o.server_banner = ask("server banner?", o.server_banner)
        o.roles = ask("roles?", o.roles)
        o.categories = ask("categories?", o.categories)
        o.channels = ask("channels?", o.channels)
        o.settings = ask("channel settings (topic/slowmode/nsfw/bitrate/limit)?", o.settings)
        o.permissions = ask("permission overwrites?", o.permissions)
        o.emojis = ask("emojis?", o.emojis)
        o.stickers = ask("stickers?", o.stickers)
    elif args.interactive:
        print("[!] --interactive: stdin TTY nahi hai (dashboard?), ignore kar raha hoon", flush=True)
    if args.pick and not tty:
        print("[!] --pick: stdin TTY nahi hai (dashboard?), ignore kar raha hoon", flush=True)
    return o


def ask(prompt: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    try:
        v = input(prompt + f" {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not v else v in ("y", "yes")


def pick(items: list, title: str) -> list:
    print(f"\n=== {title} ({len(items)}) ===", flush=True)
    for i, it in enumerate(items, 1):
        print(f"  {i:>3}. {it}", flush=True)
    raw = input("numbers daalo (1,2,5-8) | 'all' | 'none' [all]: ").strip().lower()
    if raw in ("", "all", "a"):
        return None
    if raw in ("none", "n", "no", "0"):
        return []
    chosen = []
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
        die("source aur target same server hain. Target ke liye ek alag server chahiye (bot servers create nahi kar sakta).")
    g = dst.me.guild_permissions
    warns = []
    if o.roles and not g.manage_roles:
        warns.append("target me bot ke 'Manage Roles' nahi -> roles skip honge")
    if (o.channels or o.categories) and not g.manage_channels:
        warns.append("target me bot ke 'Manage Channels' nahi -> channels skip honge")
    if (o.server_name or o.server_icon or o.server_banner) and not g.manage_guild:
        warns.append("target me 'Manage Server' nahi -> name/icon/banner skip hoga")
    if (o.emojis or o.stickers) and not g.manage_expressions:
        warns.append("target me 'Manage Emojis and Stickers' nahi -> emojis/stickers skip honge")
    for w in warns:
        print(f"[!] {w}", flush=True)


async def main() -> None:
    args = parse_args()
    saved = load_config(Path(args.config))

    if args.token_type == "user":
        die(
            "user token (self-bot) support hata diya gaya hai. Ye Discord ToS violation hai aur "
            "account turant ban ho jata hai. Bot token use karo (Developer Portal -> Bot -> Token)."
        )

    token = os.environ.get("DISCORD_TOKEN") or args.token or saved.get("token") or ""
    token = str(token).strip().strip('"').strip("'")
    if not token or token == "YOUR_DISCORD_BOT_TOKEN":
        die("bot token missing. --token, ya DISCORD_TOKEN env var, ya config.json me daalo.")
    if token.startswith("Bot "):
        token = token[4:].strip()

    source_raw = args.source or saved.get("source_guild_id")
    target_raw = args.target or saved.get("target_guild_id")
    if not source_raw or not target_raw:
        die("source aur target guild id dono chahiye (--source/--target ya config.json).")
    try:
        source_id, target_id = int(str(source_raw).strip()), int(str(target_raw).strip())
    except ValueError:
        die("guild ID numeric honi chahiye (Developer Portal me Developer Mode ON karke copy karo).")

    options = build_options(args, saved)

    intents = discord.Intents.none()
    intents.guilds = True
    bot = commands.Bot(command_prefix="!", intents=intents)
    state = {"done": False}

    @bot.event
    async def on_ready():
        if state["done"]:
            return
        state["done"] = True
        try:
            src = bot.get_guild(source_id)
            dst = bot.get_guild(target_id)
            if src is None:
                die(f"source guild {source_id} bot ki cache me nahi. Bot ko source server me invite karo.")
            if dst is None:
                die(f"target guild {target_id} bot ki cache me nahi. Bot ko target server me invite karo.")

            print(f"[+] connected as {bot.user} (id {bot.user.id})", flush=True)
            print(
                f"[i] source: {src.name} ({src.id}) | {len(src.channels)} channels, {len(src.roles)} roles",
                flush=True,
            )
            print(
                f"[i] target: {dst.name} ({dst.id}) | {len(dst.channels)} channels, {len(dst.roles)} roles",
                flush=True,
            )
            print(f"[i] options: {options.dump()}", flush=True)

            check_perms(src, dst, options)

            if args.pick and sys.stdin.isatty():
                if options.roles:
                    options.only_roles = pick([r.name for r in src.roles if not r.is_default()], "ROLES")
                if options.channels or options.categories:
                    options.only_channels = pick([c.name for c in src.channels], "CHANNELS + CATEGORIES")
                if options.emojis:
                    options.only_emojis = pick([e.name for e in src.emojis if e.name], "EMOJIS")

            if options.only_roles is not None:
                print(f"[i] only roles: {options.only_roles}", flush=True)
            if options.only_channels is not None:
                print(f"[i] only channels: {options.only_channels}", flush=True)

            if args.dry_run:
                print("\n--- DRY RUN: kuch nahi banaya jaayega ---", flush=True)
                print(f"roles    : {len([r for r in src.roles if not r.is_default()])}", flush=True)
                print(f"channels : {len(list(src.channels))}", flush=True)
                print(f"emojis   : {len([e for e in src.emojis if e.name]) if options.emojis else 0}", flush=True)
                print(f"stickers : {len(list(src.stickers)) if options.stickers else 0}", flush=True)
                print(f"target me abhi: {[c.name for c in dst.channels]}", flush=True)
                return

            if not args.yes:
                if not ask(
                    f"\n[?] {dst.name} me clone start karein? (target ke existing channels delete NAHI honge)",
                    default=False,
                ):
                    print("cancel.", flush=True)
                    return

            report = await ServerCloner(src, dst, options).run()
            print(report.dump(), flush=True)
            if report.notes:
                print("\nnotes:", flush=True)
                for n in report.notes:
                    print(f"  - {n}", flush=True)
            print(f"\n[+] done -> https://discord.com/channels/{dst.id}/@me", flush=True)
        except SystemExit:
            raise
        except discord.LoginFailure:
            die("token invalid hai (LoginFailure). Developer Portal se naya token copy karo.")
        except discord.Forbidden as e:
            die(f"Forbidden: {e} - bot ko permissions ya access missing hai.")
        except Exception as e:
            print(f"[X] {type(e).__name__}: {e}", flush=True)
            raise SystemExit(1)
        finally:
            await bot.close()

    try:
        await bot.start(token)
    except discord.LoginFailure:
        die("token invalid hai (LoginFailure). Developer Portal se naya token copy karo.")
    except discord.HTTPException as e:
        die(f"Discord connect fail: {e}")
    finally:
        http = getattr(bot, "http", None)
        if http is not None:
            try:
                await http.close()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted.", flush=True)
        raise SystemExit(130)
    except SystemExit:
        raise
