import inspect
import sys
from types import SimpleNamespace

import discord

sys.path.insert(0, ".")
from cloner import CloneOptions, ServerCloner  # noqa: E402

TARGET_METHODS = {
    "create_text_channel": discord.Guild.create_text_channel,
    "create_voice_channel": discord.Guild.create_voice_channel,
    "create_stage_channel": discord.Guild.create_stage_channel,
    "create_news_channel": getattr(discord.Guild, "create_news_channel", None),
    "create_forum_channel": getattr(discord.Guild, "create_forum_channel", None),
    "create_forum": getattr(discord.Guild, "create_forum", None),
    "create_category": discord.Guild.create_category,
}
TARGET_METHODS = {k: v for k, v in TARGET_METHODS.items() if v is not None}

TYPES = {
    "text": discord.ChannelType.text,
    "news": discord.ChannelType.news,
    "voice": discord.ChannelType.voice,
    "stage": discord.ChannelType.stage_voice,
    "forum": discord.ChannelType.forum,
    "media": discord.ChannelType.media,
}


def make_role(rid, name):
    role = discord.Role.__new__(discord.Role)
    role.id = rid
    role.name = name
    return role


class FakeGuild:
    def __init__(self, gid, name, default_role):
        self.id = gid
        self.name = name
        self.channels = []
        self.roles = []
        self.default_role = default_role
        self.bitrate_limit = 96000
        self._attrs = {}
        for k, v in TARGET_METHODS.items():
            setattr(self, k, v)

    def __getattr__(self, item):
        if item in self._attrs:
            return self._attrs[item]
        raise AttributeError(item)


def make_source_ch(name, ctype, category_id=None, **extra):
    return SimpleNamespace(
        id=abs(hash(name)) % 10**18,
        name=name,
        type=ctype,
        position=extra.pop("position", 0),
        category_id=category_id,
        topic=extra.pop("topic", "hello topic"),
        nsfw=extra.pop("nsfw", False),
        slowmode_delay=extra.pop("slowmode_delay", 30),
        bitrate=extra.pop("bitrate", 128000),
        user_limit=extra.pop("user_limit", 10),
        overwrites=extra.pop("overwrites", {}),
        **extra,
    )


def sig_without_self(fn):
    params = [p for name, p in inspect.signature(fn).parameters.items() if name != "self"]
    return inspect.Signature(params)


failures = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}{'' if cond else ' :: ' + detail}")
    if not cond:
        failures.append(label)


everyone_src = make_role(111, "@everyone")
everyone_dst = make_role(222, "@everyone")
admin_src = make_role(999, "Admin")
admin_dst = make_role(1234, "Admin")
ow_admin = discord.PermissionOverwrite.from_pair(
    discord.Permissions(administrator=True), discord.Permissions(0)
)
ow_everyone = discord.PermissionOverwrite.from_pair(discord.Permissions(0), discord.Permissions(0))

src = FakeGuild(111, "Source", everyone_src)
dst = FakeGuild(222, "Target", everyone_dst)

c = ServerCloner(src, dst, CloneOptions())
c.role_map = {999: admin_dst}
mapped = c.overwrites_for(make_source_ch("x", TYPES["text"], overwrites={everyone_src: ow_everyone, admin_src: ow_admin}))
ids = sorted(getattr(k, "id", -1) for k in mapped)
check("overwrites: @everyone + role dono mapped", ids == [222, 1234], str(ids))
check(
    "overwrites: unknown role skipped",
    sorted(getattr(k, "id", -1) for k in ServerCloner(src, dst, CloneOptions()).overwrites_for(
        make_source_ch("x", TYPES["text"], overwrites={admin_src: ow_admin})
    ))
    == [],
)
check(
    "overwrites: permissions=False -> {}",
    ServerCloner(src, dst, CloneOptions(permissions=False)).overwrites_for(
        make_source_ch("x", TYPES["text"], overwrites={admin_src: ow_admin})
    )
    == {},
)

print()
for label, ctype in TYPES.items():
    for with_settings in (True, False):
        for with_perms in (True, False):
            o = CloneOptions(settings=with_settings, permissions=with_perms)
            cl = ServerCloner(src, dst, o)
            cl.role_map = {999: admin_dst}
            fn, kwargs, overwrites, post = cl._build_create_call(
                make_source_ch(
                    f"chan-{label}",
                    ctype,
                    nsfw=True,
                    slowmode_delay=60,
                    overwrites={everyone_src: ow_everyone, admin_src: ow_admin},
                ),
                make_role(4321, "cat"),
            )
            try:
                sig_without_self(fn).bind(**kwargs)
                valid, detail = True, ""
            except TypeError as e:
                valid, detail = False, str(e)
            tag = f"{label:<6} settings={str(with_settings):<5} perms={str(with_perms):<5}"
            check(tag, valid, detail)
            if label == "stage" and with_settings:
                check(f"{tag} topic handled", kwargs.get("topic") or post.get("topic"), str(kwargs) + str(post))

            applied_at_create = "overwrites" in kwargs
            applied_after_create = bool(overwrites) and not applied_at_create
            check(
                f"{tag} overwrites wired",
                (applied_at_create or applied_after_create) == with_perms,
                str(list(kwargs)),
            )
            if with_settings:
                if label in ("voice", "stage"):
                    check(f"{tag} settings carried", True)
                else:
                    check(
                        f"{tag} settings carried",
                        kwargs.get("nsfw") is True and kwargs.get("slowmode_delay") == 60,
                        str(kwargs),
                    )
                if label == "voice":
                    check(f"{tag} bitrate clamped", kwargs.get("bitrate") == 96000, str(kwargs))
            else:
                check(
                    f"{tag} no settings leaked",
                    not {"topic", "nsfw", "slowmode_delay", "bitrate", "user_limit"} & set(kwargs),
                    str(kwargs),
                )
                check(f"{tag} no post edits", not post, str(post))
            if label == "news":
                check(f"{tag} announcement type kept", "news" in kwargs, str(kwargs))
            if label == "media":
                check(f"{tag} media type kept", kwargs.get("media") is True, str(kwargs))

print()
check(
    "text channel does not go through category builder",
    ServerCloner(src, dst, CloneOptions())
    ._build_create_call(make_source_ch("c", TYPES["text"]), None)[0]
    .__name__
    != "create_category",
)
o = CloneOptions()
check("want() None = all", len(o.want(["a", "b"], None)) == 2)
check("want() list filter", o.want(["Alpha", "Beta"], ["alpha"]) == ["Alpha"])
check("want() [] = nothing", o.want(["Alpha", "Beta"], []) == [])
check("emoji name rejects 1 char", not ServerCloner._valid_emoji_name("a"))
check("emoji name rejects space", not ServerCloner._valid_emoji_name("a b"))
check("emoji name accepts underscores", ServerCloner._valid_emoji_name("cool_emoji_1"))
check("emoji name rejects None", not ServerCloner._valid_emoji_name(None))

print()
print("ALL PASS" if not failures else f"FAILURES ({len(failures)}): " + ", ".join(failures))
sys.exit(1 if failures else 0)
