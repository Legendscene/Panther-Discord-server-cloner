import asyncio
import inspect
import io

import aiohttp
import discord

_SKIP_NAMES = ("threads", "private_thread", "news_thread", "public_thread", "private")
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
        ]:
            parts.append(f"{label}={'Y' if flag else 'n'}")
        return " ".join(parts)


class CloneReport:
    def __init__(self):
        self.roles = self.channels = self.emojis = self.stickers = {
            "ok": 0,
            "skip": 0,
            "fail": 0,
        }
        self.notes: list = []

    def bump(self, bucket, key):
        bucket[key] = bucket.get(key, 0) + 1

    def dump(self) -> str:
        def line(name, b):
            return f"{name:<10} ok:{b['ok']:<4} skip:{b['skip']:<4} fail:{b['fail']}"

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
        self.pre_existing = len([c for c in target.channels if c.type not in SKIP_CHANNEL_TYPES])
        self.created_order: list = []

    async def run(self) -> CloneReport:
        await self.clone_guild_meta()
        await self.clone_roles()
        await self.clone_channels()
        await self.fix_positions()
        if self.options.emojis:
            await self.clone_emojis()
        if self.options.stickers:
            await self.clone_stickers()
        return self.report

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
            print(f"[+] server meta cloned: {', '.join(kwargs)}")
        except discord.HTTPException as e:
            self.report.notes.append(f"server meta failed: {e}")

    @staticmethod
    async def _read_asset(asset):
        try:
            return await asset.read()
        except Exception as e:
            print(f"[!] asset read failed: {e}")
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
            print("[i] no roles available to copy, skipping")
            return
        print(f"[i] copying {len(wanted)} roles ...")
        for role in wanted:
            if role.managed or role.is_bot_managed():
                self.report.bump(self.report.roles, "skip")
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
                print(f"    + {role.name}")
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
            if obj.id == self.source.id:
                key = self.target.default_role
            elif isinstance(obj, discord.Role):
                key = self.role_map.get(obj.id)
            else:
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
        categories: dict = {}
        for c in ordered:
            if c.type is TYPE_CATEGORY:
                if not self.options.categories:
                    categories[c.id] = None
                    continue
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
            created = await self._create_channel(src, parent)
            if created is not None:
                self.created_order.append((src, created))
                self.report.bump(self.report.channels, "ok")
            await asyncio.sleep(0.35)

    async def _create_category(self, src):
        try:
            new = await self.target.create_category(
                name=src.name,
                overwrites=self.overwrites_for(src),
                reason="server clone",
            )
            print(f"    + [category] {src.name}")
            return new
        except discord.HTTPException as e:
            self.report.bump(self.report.channels, "fail")
            self.report.notes.append(f"category '{src.name}': {e}")
            return None

    def _settings_kwargs(self, src) -> dict:
        kwargs = {"name": src.name}
        if not self.options.settings:
            return kwargs
        if getattr(src, "topic", None):
            kwargs["topic"] = src.topic[:1024]
        if getattr(src, "slowmode_delay", 0):
            kwargs["slowmode_delay"] = src.slowmode_delay
        if src.nsfw:
            kwargs["nsfw"] = True
        return kwargs

    def _build_create_call(self, src, parent):
        overwrites = self.overwrites_for(src)
        settings = self.options.settings
        base = {"reason": "server clone"}
        if parent is not None:
            base["category"] = parent
        post: dict = {}
        topic = (src.topic or None) if src.topic else None

        if src.type is TYPE_STAGE:
            fn = getattr(self.target, "create_stage_channel")
            kwargs = {"name": src.name, **base}
            if topic and settings:
                if _has_kwarg(fn, "topic"):
                    kwargs["topic"] = topic[:1024]
                else:
                    post["topic"] = topic[:1024]
        elif src.type in (TYPE_FORUM, TYPE_MEDIA):
            fn = getattr(self.target, "create_forum_channel", None) or getattr(
                self.target, "create_forum"
            )
            kwargs = {"name": src.name, **base}
            if topic and settings:
                kwargs["topic"] = topic[:1024]
            if settings and _has_kwarg(fn, "slowmode_delay"):
                kwargs["slowmode_delay"] = src.slowmode_delay or 0
            if settings and _has_kwarg(fn, "nsfw"):
                kwargs["nsfw"] = src.nsfw
            if _has_kwarg(fn, "media") and src.type is TYPE_MEDIA:
                kwargs["media"] = True
        elif src.type is TYPE_NEWS:
            fn = getattr(self.target, "create_news_channel", None)
            if fn is not None:
                kwargs = {"name": src.name, **base}
                if topic and settings:
                    kwargs["topic"] = topic[:1024]
            else:
                fn = getattr(self.target, "create_text_channel")
                kwargs = {**self._settings_kwargs(src), "news": True, **base}
        elif src.type is TYPE_VOICE:
            fn = getattr(self.target, "create_voice_channel")
            kwargs = {"name": src.name, **base}
            if settings and _has_kwarg(fn, "bitrate"):
                kwargs["bitrate"] = min(src.bitrate or 0, self.target.bitrate_limit)
            if settings and _has_kwarg(fn, "user_limit"):
                kwargs["user_limit"] = src.user_limit or 0
        else:
            fn = getattr(self.target, "create_text_channel")
            kwargs = {**self._settings_kwargs(src), **base}

        if overwrites and _has_kwarg(fn, "overwrites"):
            kwargs["overwrites"] = overwrites
        return fn, kwargs, overwrites, post

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
        print(f"    + #{src.name} [{src.type.name}]")
        return new

    async def fix_positions(self):
        if not self.created_order:
            return
        ordered = sorted(self.created_order, key=lambda pair: pair[0].position)
        for index, (src, ch) in enumerate(ordered):
            try:
                await ch.edit(position=self.pre_existing + index)
            except discord.HTTPException as e:
                self.report.notes.append(f"position '{ch.name}': {e}")
            await asyncio.sleep(0.12)

    async def clone_emojis(self):
        wanted = self.options.want(
            [e for e in self.source.emojis if self._valid_emoji_name(e.name)], self.options.only_emojis
        )
        if not wanted:
            print("[i] no emojis available to copy, skipping")
            return
        print(f"[i] importing {len(wanted)} emojis ...")
        async with aiohttp.ClientSession() as session:
            for emoji in wanted:
                try:
                    data = await self._download(session, str(emoji.url))
                    new = await self.target.create_custom_emoji(
                        name=emoji.name, image=data, reason="server clone"
                    )
                    self.emojis_created.append(new)
                    self.report.bump(self.report.emojis, "ok")
                    print(f"    + :{emoji.name}:")
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
            print("[i] no stickers available to copy, skipping")
            return
        print(f"[i] importing {len(wanted)} stickers ...")
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
                    print(f"    + sticker {sticker.name}")
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
        try:
            await self.target.create_sticker(
                **payload, file=discord.File(io.BytesIO(data), filename=f"{sticker.name}.{ext}")
            )
        except TypeError:
            await self.target.create_sticker(**payload, image=data)

    def _sticker_emoji(self) -> str:
        for e in self.emojis_created:
            return f"<a:{e.name}:{e.id}>" if e.animated else f"<:{e.name}:{e.id}>"
        return "\U0001f642"

    @staticmethod
    async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()
