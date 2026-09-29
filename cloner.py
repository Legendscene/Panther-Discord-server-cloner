import asyncio
import inspect
import io
from typing import Any

import aiohttp
import discord

_SKIP_NAMES = ("threads", "private_thread", "news_thread", "public_thread", "private", "guild_directory")
SKIP_CHANNEL_TYPES = {
    t for t in (getattr(discord.ChannelType, n, None) for n in _SKIP_NAMES) if t is not None
}


def _chtype(*names):
    for n in names:
        t = getattr(discord.ChannelType, n, None)
        if t is not None:
            return t
    return None


TYPE_STAGE = _chtype("stage_channel", "stage_voice")
TYPE_NEWS = _chtype("news")
TYPE_FORUM = _chtype("forum")
TYPE_MEDIA = _chtype("media")
TYPE_VOICE = _chtype("voice")
TYPE_CATEGORY = _chtype("category")


def _has_kwarg(func, name) -> bool:
    try:
        return name in func.__code__.co_varnames
    except AttributeError:
        return False


class CloneOptions:
    def __init__(
        self,
        server_name=True,
        server_icon=True,
        server_banner=False,
        roles=True,
        categories=True,
        channels=True,
        settings=True,
        permissions=True,
        emojis=False,
        stickers=False,
        clean_target=True,
        only_roles=None,
        only_channels=None,
        only_emojis=None,
    ):
        self.server_name = server_name
        self.server_icon = server_icon
        self.server_banner = server_banner
        self.roles = roles
        self.categories = categories
        self.channels = channels
        self.settings = settings
        self.permissions = permissions
        self.emojis = emojis
        self.stickers = stickers
        self.clean_target = clean_target
        self.only_roles = only_roles
        self.only_channels = only_channels
        self.only_emojis = only_emojis

    def want(self, values, only):
        if only is None:
            return list(values)
        wanted = {str(o).lower().lstrip("#") for o in only}
        return [v for v in values if str(v).lower() in wanted]

    def dump(self) -> str:
        parts = []
        for label, flag in [
            ("name", self.server_name),
            ("icon", self.server_icon),
            ("banner", self.server_banner),
            ("roles", self.roles),
            ("categories", self.categories),
            ("channels", self.channels),
            ("settings", self.settings),
            ("permissions", self.permissions),
            ("emojis", self.emojis),
            ("stickers", self.stickers),
            ("clean", self.clean_target),
        ]:
            parts.append(f"{label}={'Y' if flag else 'n'}")
        return " ".join(parts)


class CloneReport:
    def __init__(self):
        self.roles = self.channels = self.emojis = self.stickers = {
            "ok": 0,
            "reuse": 0,
            "skip": 0,
            "fail": 0,
        }
        self.notes: list = []

    def bump(self, bucket, key):
        bucket[key] = bucket.get(key, 0) + 1

    def dump(self) -> str:
        def line(name, b):
            return (
                f"{name:<10} ok:{b['ok']:<4} reuse:{b.get('reuse', 0):<4} "
                f"skip:{b['skip']:<4} fail:{b['fail']}"
            )

        return "\n".join(
            [
                "",
                line("roles", self.roles),
                line("channels", self.channels),
                line("emojis", self.emojis),
                line("stickers", self.stickers),
            ]
        )


class ServerCloner:
    def __init__(self, source: discord.Guild, target: discord.Guild, options: CloneOptions):
        self.source = source
        self.target = target
        self.options = options
        self.report = CloneReport()
        self.role_map: dict = {}
        self.emojis_created: list = []
        self.missing_overwrite_roles: set = set()
        self.pre_existing = len([c for c in target.channels if c.type not in SKIP_CHANNEL_TYPES])
        self.created_order: list = []

    @staticmethod
    async def _ensure_guild_fetched(guild: discord.Guild) -> None:
        """User-token sessions only receive guild skeletons over the gateway;
        roles/channels/expressions are fetched over HTTP the first time a guild
        is used. Bot sessions already have everything cached, so this is a no-op
        there."""
        if guild._roles and guild._channels:  # already populated
            return
        state = guild._state
        try:
            data = await state.http.get_guild(guild.id)
        except discord.HTTPException as e:
            raise discord.ClientException(
                f"Could not load guild {guild.id} over REST ({e}). "
                "The account may not be a member of that server."
            ) from e
        guild._from_data(data)

    async def run(self) -> CloneReport:
        await self._ensure_guild_fetched(self.source)
        await self._ensure_guild_fetched(self.target)
        # Each phase is individually guarded: an unexpected error in one phase
        # (weird channel type, API oddity, attribute the library does not set)
        # becomes a report note instead of killing the whole run.
        phases = [
            ("clean target", self._phase_clean),
            ("server meta", self._phase_meta),
            ("roles", self._phase_roles),
            ("channels", self._phase_channels),
            ("positions", self._phase_positions),
            ("emojis", self._phase_emojis),
            ("stickers", self._phase_stickers),
        ]
        for name, phase in phases:
            try:
                await phase()
            except discord.HTTPException as e:
                self.report.notes.append(f"{name} phase failed: {e}")
                print(f"[!] {name} phase failed: {e}", flush=True)
            except discord.ClientException as e:
                self.report.notes.append(f"{name} phase failed: {e}")
                print(f"[!] {name} phase failed: {e}", flush=True)
            except Exception as e:  # noqa: BLE001 - never let one phase kill the run
                self.report.notes.append(f"{name} phase crashed: {type(e).__name__}: {e}")
                print(f"[X] {name} phase crashed: {type(e).__name__}: {e}", flush=True)
        return self.report

    async def _phase_clean(self):
        if self.options.clean_target:
            await self.clean_target_guild()

    async def _phase_meta(self):
        await self.clone_guild_meta()

    async def _phase_roles(self):
        await self.clone_roles()

    async def _phase_channels(self):
        await self.clone_channels()

    async def _phase_positions(self):
        await self.fix_positions()

    async def _phase_emojis(self):
        if self.options.emojis:
            await self.clone_emojis()

    async def _phase_stickers(self):
        if self.options.stickers:
            await self.clone_stickers()

    async def clean_target_guild(self):
        """Delete every cloneable channel and every unmanaged role on the
        target before cloning, so the result mirrors the source exactly instead
        of accumulating leftovers from previous runs. @everyone and managed
        (integration/bot) roles are always kept - Discord forbids deleting
        them."""
        channel_count = len(
            [c for c in getattr(self.target, "channels", []) if c.type not in SKIP_CHANNEL_TYPES]
        )
        target_name = getattr(self.target, "name", "") or "unknown"
        print(
            f"[!] clean target: deleting {channel_count} channels and roles from '{target_name}' ...",
            flush=True,
        )

        # Channels first (children before their categories, since deleting a
        # category would fail while children still reference it).
        channels = sorted(
            (c for c in self.target.channels if c.type not in SKIP_CHANNEL_TYPES),
            key=lambda c: (c.category_id is not None, c.category_id or 0),
            reverse=True,
        )
        for channel in channels:
            try:
                await channel.delete(reason="server clone: pre-clean target")
                self.report.bump(self.report.channels, "skip")
                print(f"    - #{channel.name} [{channel.type.name}] deleted", flush=True)
            except discord.HTTPException as e:
                self.report.notes.append(f"clean channel '{channel.name}': {e}")
                print(f"    [X] #{channel.name}: {e}", flush=True)
            await asyncio.sleep(0.25)

        # Roles: skip @everyone (undeletable) and managed roles.
        for role in self.target.roles:
            if role.is_default() or role.managed:
                continue
            try:
                await role.delete(reason="server clone: pre-clean target")
                self.report.bump(self.report.roles, "skip")
                print(f"    - @{role.name} deleted", flush=True)
            except discord.HTTPException as e:
                self.report.notes.append(f"clean role '{role.name}': {e}")
                print(f"    [X] @{role.name}: {e}", flush=True)
            await asyncio.sleep(0.25)

        # Refresh the cached target so position bookkeeping starts from zero.
        self.pre_existing = 0
        print("[+] clean target done", flush=True)

    async def clone_guild_meta(self):
        kwargs = {}
        if self.options.server_name and self.source.name:
            kwargs["name"] = self.source.name
        if self.options.server_icon and self.source.icon:
            data = await self._read_asset(self.source.icon)
            if data:
                kwargs["icon"] = data
        if self.options.server_banner and self.source.banner:
            data = await self._read_asset(self.source.banner)
            if data:
                kwargs["banner"] = data
        if not kwargs:
            return
        try:
            await self.target.edit(**kwargs)
            print(f"[+] server meta cloned: {', '.join(kwargs)}", flush=True)
        except discord.Forbidden as e:
            # User accounts need an explicit MFA step for privileged guild edits.
            raise discord.ClientException(
                "Target edit forbidden. If 2FA is enabled on your account, send any message "
                "in the target server from the Discord app once, then retry (Discord MFA step-up)."
            ) from e
        except discord.HTTPException as e:
            self.report.notes.append(f"server meta failed: {e}")

    @staticmethod
    async def _read_asset(asset):
        try:
            return await asset.read()
        except Exception as e:
            print(f"[!] asset read failed: {e}", flush=True)
            return None

    def source_roles(self) -> list:
        return sorted(
            (r for r in self.source.roles if not r.is_default()),
            key=lambda r: r.position,
            reverse=True,
        )

    async def clone_roles(self):
        if not self.options.roles:
            return
        wanted = self.options.want(self.source_roles(), self.options.only_roles)
        if not wanted:
            print("[+] no roles available to copy, skipping", flush=True)
            return
        print(f"[+] copying {len(wanted)} roles ...", flush=True)

        # Resume support: index the target's existing roles by name so a
        # re-run (clean_target=False) reuses them instead of creating
        # duplicates. Wipe mode never reaches this because everything was
        # just deleted.
        existing_roles: dict = {}
        if not self.options.clean_target:
            for r in self.target.roles:
                if not r.is_default() and not r.managed:
                    existing_roles.setdefault(r.name, r)

        for role in wanted:
            if role.managed or role.is_bot_managed():
                self.report.bump(self.report.roles, "skip")
                continue
            if role.name in existing_roles:
                new = existing_roles.pop(role.name)
                self.role_map[role.id] = new
                self.report.bump(self.report.roles, "reuse")
                print(f"    = {role.name} (reused)", flush=True)
                continue
            try:
                new = await self.target.create_role(
                    name=role.name,
                    permissions=role.permissions,
                    colour=role.colour,
                    hoist=role.hoist,
                    mentionable=role.mentionable,
                    reason="server clone",
                )
                self.role_map[role.id] = new
                self.report.bump(self.report.roles, "ok")
                print(f"    + {role.name}", flush=True)
            except discord.HTTPException as e:
                self.report.bump(self.report.roles, "fail")
                self.report.notes.append(f"role '{role.name}': {e}")
            await asyncio.sleep(0.4)

    def overwrites_for(self, src_channel) -> dict:
        if not self.options.permissions:
            return {}
        result: dict = {}
        for obj, perm in src_channel.overwrites.items():
            allow, deny = perm.pair()
            key: Any = None
            if obj.id == self.source.id:
                key = self.target.default_role
            elif isinstance(obj, discord.Role):
                key = self.role_map.get(obj.id)
                if key is None:
                    # The source role could not be cloned (managed/integration
                    # or creation failed); track it so the report can say so.
                    self.missing_overwrite_roles.add(getattr(obj, "name", str(obj.id)))
            else:
                # Member-specific overwrites are tied to individual user IDs.
                continue
            if key is None:
                continue
            result[key] = discord.PermissionOverwrite.from_pair(allow, deny)
        return result

    def channel_wanted(self, channel) -> bool:
        only = self.options.only_channels
        if only is None:
            return True
        return (channel.name or "").lower() in {o.lower().lstrip("#") for o in only}

    async def clone_channels(self):
        if not self.options.channels:
            return
        ordered = sorted(
            (c for c in self.source.channels if c.type not in SKIP_CHANNEL_TYPES),
            key=lambda c: c.position,
        )

        # Resume support: index the target's existing channels by
        # (type, name) so a re-run (clean_target=False) reuses them instead
        # of creating duplicates. Categories must be matched first because
        # regular channels attach to them.
        existing_categories: dict = {}
        existing_channels: dict = {}
        if not self.options.clean_target:
            for c in self.target.channels:
                if c.type in SKIP_CHANNEL_TYPES:
                    continue
                if c.type is TYPE_CATEGORY:
                    existing_categories.setdefault(c.name, c)
                else:
                    existing_channels.setdefault((c.type, c.name), c)

        categories: dict = {}
        for c in ordered:
            if c.type is TYPE_CATEGORY:
                if not self.options.categories:
                    categories[c.id] = None
                    continue
                if c.name in existing_categories:
                    new_cat = existing_categories.pop(c.name)
                    self.report.bump(self.report.channels, "reuse")
                    print(f"    = [category] {c.name} (reused)", flush=True)
                else:
                    new_cat = await self._create_category(c)
                categories[c.id] = new_cat
                if new_cat is not None:
                    self.created_order.append((c, new_cat))

        for src in ordered:
            if src.type is TYPE_CATEGORY:
                continue
            if not self.channel_wanted(src):
                self.report.bump(self.report.channels, "skip")
                continue
            parent = categories.get(src.category_id)
            reuse_key = (src.type, src.name)
            if reuse_key in existing_channels:
                created = existing_channels.pop(reuse_key)
                self.report.bump(self.report.channels, "reuse")
                print(f"    = #{src.name} [{src.type.name}] (reused)", flush=True)
                # Keep an existing channel's settings/permissions in sync with
                # the source so a resumed clone still ends up identical.
                await self._sync_reused_channel(src, created, parent)
            else:
                created = await self._create_channel(src, parent)
                if created is not None:
                    self.report.bump(self.report.channels, "ok")
            if created is not None:
                self.created_order.append((src, created))
                await asyncio.sleep(0.35)

    async def _create_category(self, src):
        try:
            new = await self.target.create_category(
                name=src.name,
                overwrites=self.overwrites_for(src),
                reason="server clone",
            )
            print(f"    + [category] {src.name}", flush=True)
            return new
        except discord.HTTPException as e:
            self.report.bump(self.report.channels, "fail")
            self.report.notes.append(f"category '{src.name}': {e}")
            return None

    def _settings_kwargs(self, src) -> dict:
        kwargs = {"name": src.name}
        if not self.options.settings:
            return kwargs
        topic = getattr(src, "topic", None)
        if topic:
            kwargs["topic"] = topic[:1024]
        slowmode = getattr(src, "slowmode_delay", 0)
        if slowmode:
            kwargs["slowmode_delay"] = slowmode
        if getattr(src, "nsfw", False):
            kwargs["nsfw"] = True
        return kwargs

    def _build_create_call(self, src, parent):
        overwrites = self.overwrites_for(src)
        settings = self.options.settings
        base = {"reason": "server clone"}
        if parent is not None:
            base["category"] = parent
        post: dict = {}
        # Voice/stage channels have no .topic attribute; read it defensively so
        # any channel type can pass through this builder safely.
        topic = getattr(src, "topic", None)

        if src.type is TYPE_STAGE:
            fn = self.target.create_stage_channel
            kwargs = {"name": src.name, **base}
            if topic and settings:
                if _has_kwarg(fn, "topic"):
                    kwargs["topic"] = topic[:1024]
                else:
                    post["topic"] = topic[:1024]
        elif src.type in (TYPE_FORUM, TYPE_MEDIA):
            fn = self._forum_creator()
            kwargs = {"name": src.name, **base}
            if topic and settings:
                kwargs["topic"] = topic[:1024]
            if settings and _has_kwarg(fn, "slowmode_delay"):
                kwargs["slowmode_delay"] = getattr(src, "slowmode_delay", 0) or 0
            if settings and _has_kwarg(fn, "nsfw"):
                kwargs["nsfw"] = bool(getattr(src, "nsfw", False))
            if _has_kwarg(fn, "media") and src.type is TYPE_MEDIA:
                kwargs["media"] = True
        elif src.type is TYPE_NEWS:
            fn = getattr(self.target, "create_news_channel", None)
            if fn is not None:
                kwargs = {"name": src.name, **base}
                if topic and settings:
                    kwargs["topic"] = topic[:1024]
            else:
                fn = self.target.create_text_channel
                kwargs = {**self._settings_kwargs(src), "news": True, **base}
        elif src.type is TYPE_VOICE:
            fn = self.target.create_voice_channel
            kwargs = {"name": src.name, **base}
            if settings and _has_kwarg(fn, "bitrate"):
                kwargs["bitrate"] = min(getattr(src, "bitrate", 0) or 0, self.target.bitrate_limit)
            if settings and _has_kwarg(fn, "user_limit"):
                kwargs["user_limit"] = getattr(src, "user_limit", 0) or 0
        else:
            fn = self.target.create_text_channel
            kwargs = {**self._settings_kwargs(src), **base}

        if overwrites and _has_kwarg(fn, "overwrites"):
            kwargs["overwrites"] = overwrites
        return fn, kwargs, overwrites, post

    def _forum_creator(self):
        creator = getattr(self.target, "create_forum_channel", None)
        if creator is not None:
            return creator
        return self.target.create_forum

    @staticmethod
    def _sanitize(fn, kwargs: dict) -> tuple:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return kwargs, set()
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return kwargs, set()
        dropped = {k for k in kwargs if k not in params}
        for k in dropped:
            kwargs.pop(k)
        return kwargs, dropped

    async def _sync_reused_channel(self, src, existing, parent):
        """Resume mode: refresh an existing channel's topic/slowmode/nsfw and
        permission overwrites to match the source. Failures are notes only."""
        try:
            kwargs: dict = {}
            if self.options.settings:
                topic = getattr(src, "topic", None)
                if topic:
                    kwargs["topic"] = topic[:1024]
                slowmode = getattr(src, "slowmode_delay", 0)
                if slowmode:
                    kwargs["slowmode_delay"] = slowmode
                if getattr(src, "nsfw", False):
                    kwargs["nsfw"] = True
                user_limit = getattr(src, "user_limit", 0)
                if src.type is TYPE_VOICE and user_limit:
                    kwargs["user_limit"] = user_limit
            if parent is not None:
                kwargs["category"] = parent
            if self.options.permissions:
                ow = self.overwrites_for(src)
                if ow:
                    kwargs["overwrites"] = ow
            if kwargs:
                await existing.edit(**kwargs, reason="server clone: resume sync")
                print("      synced settings/permissions", flush=True)
        except discord.HTTPException as e:
            self.report.notes.append(f"sync '{src.name}': {e}")
        except discord.ClientException as e:
            self.report.notes.append(f"sync '{src.name}': {e}")

    async def _create_channel(self, src, parent):
        fn, kwargs, overwrites, post = self._build_create_call(src, parent)
        kwargs, dropped = self._sanitize(fn, kwargs)
        if overwrites and "overwrites" in dropped:
            post = {**post, "overwrites": overwrites}
        try:
            new = await fn(**kwargs)
        except discord.HTTPException as e:
            self.report.bump(self.report.channels, "fail")
            self.report.notes.append(f"channel '{src.name}' ({src.type.name}): {e}")
            return None

        if post:
            try:
                new = await new.edit(**post)
            except discord.HTTPException as e:
                self.report.notes.append(f"post-edit '{src.name}': {e}")
        print(f"    + #{src.name} [{src.type.name}]", flush=True)
        return new

    async def fix_positions(self):
        if not self.created_order:
            return
        # In resume mode (wipe skipped) the target keeps its original layout,
        # so re-positioning everything from 0 would scramble untouched
        # channels. Only shift the channels we actually created/used here,
        # relative to where they already are.
        base = 0 if self.options.clean_target else None
        ordered = sorted(self.created_order, key=lambda pair: pair[0].position)
        for index, (_src, ch) in enumerate(ordered):
            target_position = (base + index) if base is not None else getattr(ch, "position", index)
            try:
                await ch.edit(position=target_position)
            except discord.HTTPException as e:
                self.report.notes.append(f"position '{ch.name}': {e}")
            await asyncio.sleep(0.12)

    async def clone_emojis(self):
        wanted = self.options.want(
            [e for e in self.source.emojis if self._valid_emoji_name(e.name)], self.options.only_emojis
        )
        if not wanted:
            print("[+] no emojis available to copy, skipping", flush=True)
            return
        print(f"[+] importing {len(wanted)} emojis ...", flush=True)
        async with aiohttp.ClientSession() as session:
            for emoji in wanted:
                try:
                    data = await self._download(session, str(emoji.url))
                    new = await self.target.create_custom_emoji(
                        name=emoji.name, image=data, reason="server clone"
                    )
                    self.emojis_created.append(new)
                    self.report.bump(self.report.emojis, "ok")
                    print(f"    + :{emoji.name}:", flush=True)
                except Exception as e:
                    self.report.bump(self.report.emojis, "fail")
                    self.report.notes.append(f"emoji '{emoji.name}': {e}")
                await asyncio.sleep(0.6)

    @staticmethod
    def _valid_emoji_name(name) -> bool:
        return bool(name) and 2 <= len(name) <= 32 and name.replace("_", "").isalnum()

    async def clone_stickers(self):
        wanted = list(self.source.stickers)
        if not wanted:
            print("[+] no stickers available to copy, skipping", flush=True)
            return
        print(f"[+] importing {len(wanted)} stickers ...", flush=True)
        async with aiohttp.ClientSession() as session:
            for sticker in wanted:
                fmt = getattr(sticker, "format", None)
                if fmt is discord.StickerFormatType.LOTTIE:
                    self.report.bump(self.report.stickers, "skip")
                    self.report.notes.append(f"sticker '{sticker.name}' is a LOTTIE sticker, skipping")
                    continue
                try:
                    url = str(getattr(sticker, "url", None) or sticker.asset.url)
                    data = await self._download(session, url)
                    ext = "gif" if fmt is discord.StickerFormatType.GIF else "png"
                    await self._create_sticker(sticker, data, ext)
                    self.report.bump(self.report.stickers, "ok")
                    print(f"    + sticker {sticker.name}", flush=True)
                except Exception as e:
                    self.report.bump(self.report.stickers, "fail")
                    self.report.notes.append(f"sticker '{sticker.name}': {e}")
                await asyncio.sleep(0.8)

    async def _create_sticker(self, sticker, data: bytes, ext: str):
        payload = {
            "name": sticker.name,
            "description": (sticker.description or "")[:100],
            "emoji": self._sticker_emoji(),
            "reason": "server clone",
        }
        create_sticker: Any = self.target.create_sticker
        try:
            await create_sticker(
                **payload, file=discord.File(io.BytesIO(data), filename=f"{sticker.name}.{ext}")
            )
        except TypeError:
            await create_sticker(**payload, image=data)

    def _sticker_emoji(self) -> str:
        for e in self.emojis_created:
            return f"<a:{e.name}:{e.id}>" if e.animated else f"<:{e.name}:{e.id}>"
        return "\U0001f642"

    @staticmethod
    async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()
