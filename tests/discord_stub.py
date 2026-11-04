"""Reemplazo MÍNIMO de discord.py para poder importar y ejecutar la capa de Discord sin red.

Sirve para detectar errores de importación, de decoradores, de nombres y de lógica en los
callbacks. NO reemplaza probar contra Discord real: no valida que la API acepte lo que se envía.
"""
import enum
import sys
import types
from datetime import datetime


class _Exc(Exception):
    pass


class NotFound(_Exc):
    pass


class Forbidden(_Exc):
    pass


class HTTPException(_Exc):
    pass


class Color:
    def __init__(self, value=0):
        self.value = value

    @classmethod
    def _make(cls, v):
        return cls(v)


for _name, _val in dict(green=0x2ECC71, red=0xE74C3C, gold=0xF1C40F, blurple=0x5865F2, light_grey=0x95A5A6,
                        orange=0xE67E22, dark_grey=0x607D8B, blue=0x3498DB, yellow=0xFEE75C, purple=0x9B59B6, teal=0x1ABC9C).items():
    setattr(Color, _name, classmethod(lambda cls, v=_val: cls(v)))


class Embed:
    def __init__(self, title=None, description=None, color=None, timestamp=None, **kw):
        self.title = title
        self.description = description
        self.color = color
        self.timestamp = timestamp
        self.fields = []
        self.footer = None
        self.thumbnail = None
        self.author = None

    def add_field(self, *, name, value, inline=True):
        self.fields.append((name, value, inline))
        return self

    def set_footer(self, *, text=None, icon_url=None):
        self.footer = text
        return self

    def set_thumbnail(self, *, url):
        self.thumbnail = url
        return self

    def set_author(self, *, name, icon_url=None, url=None):
        self.author = name
        return self

    def __len__(self):
        n = len(self.title or "") + len(self.description or "") + len(self.footer or "") + len(self.author or "")
        for name, value, _ in self.fields:
            n += len(name) + len(value)
        return n


class AllowedMentions:
    def __init__(self, **kw):
        self.kw = kw

    @classmethod
    def none(cls):
        return cls(none=True)


class Object:
    def __init__(self, id):
        self.id = id


class ButtonStyle(enum.Enum):
    primary = 1
    secondary = 2
    success = 3
    danger = 4
    link = 5


class TextStyle(enum.Enum):
    short = 1
    paragraph = 2


class SelectOption:
    def __init__(self, *, label, value, description=None, emoji=None, default=False):
        self.label, self.value, self.description, self.emoji, self.default = label, value, description, emoji, default


class Permissions:
    def __init__(self, administrator=False, **kw):
        self.administrator = administrator
        for k, v in kw.items():
            setattr(self, k, v)


class PermissionOverwrite:
    def __init__(self, **perms):
        self.perms = perms


# ---------------- entidades ----------------
class Role:
    def __init__(self, id, name="rol", guild=None):
        self.id, self.name, self.guild = id, name, guild

    @property
    def mention(self):
        return f"<@&{self.id}>"

    @property
    def members(self):
        return [m for m in self.guild.members if self in m.roles]

    def __eq__(self, other):
        return isinstance(other, Role) and other.id == self.id

    def __hash__(self):
        return hash(self.id)


class _Avatar:
    url = "https://example.invalid/a.png"


class User:
    def __init__(self, id, name="user", bot=False):
        self.id, self.name, self.bot = id, name, bot
        self.display_name = name
        self.global_name = None
        self.display_avatar = _Avatar()

    @property
    def mention(self):
        return f"<@{self.id}>"


class Member(User):
    def __init__(self, id, name="user", roles=(), guild=None, bot=False, nick=None, administrator=False):
        super().__init__(id, name, bot)
        self.roles = list(roles)
        self.guild = guild
        self.nick = nick
        self.display_name = nick or name
        self.guild_permissions = Permissions(administrator=administrator)

    async def add_roles(self, *roles, reason=None):
        for r in roles:
            if r not in self.roles:
                self.roles.append(r)

    async def remove_roles(self, *roles, reason=None):
        self.roles = [r for r in self.roles if r not in roles]

    async def edit(self, *, nick=None, reason=None, **kw):
        if getattr(self.guild, "forbid_nick", False):
            raise Forbidden("no")
        self.nick = nick
        self.display_name = nick or self.name


class Guild:
    def __init__(self, id=1, owner_id=0):
        self.id, self.owner_id = id, owner_id
        self.members = []
        self.roles = []
        self.chunked = True
        self.default_role = Role(id, "@everyone", self)
        self.me = None
        self._channels = {}

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    async def fetch_member(self, uid):
        m = self.get_member(uid)
        if m is None:
            raise NotFound()
        return m

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def chunk(self, cache=True):
        self.chunked = True

    async def create_text_channel(self, name, *, category=None, overwrites=None, topic=None, **kw):
        if getattr(self, "fail_create_channel", False):
            raise Forbidden("sin permiso")
        Guild._next_channel = getattr(Guild, "_next_channel", 7000000000000000000) + 1
        ch = TextChannel(Guild._next_channel, self, name)
        ch.category, ch.overwrites, ch.topic = category, overwrites or {}, topic
        self._channels[ch.id] = ch
        self.created_channels = getattr(self, "created_channels", []) + [ch]
        return ch

    async def create_voice_channel(self, name, *, category=None, overwrites=None, reason=None, **kw):
        if getattr(self, "fail_create_channel", False):
            raise Forbidden("sin permiso")
        Guild._next_channel = getattr(Guild, "_next_channel", 7000000000000000000) + 1
        ch = VoiceChannel(Guild._next_channel, self, name)
        ch.category, ch.overwrites = category, overwrites or {}
        self._channels[ch.id] = ch
        self.created_channels = getattr(self, "created_channels", []) + [ch]
        return ch


class Message:
    _next = 1000

    def __init__(self, channel=None, content="", embeds=None, view=None, author=None):
        Message._next += 1
        self.id = Message._next
        self.channel, self.content, self.embeds, self.view, self.author = channel, content, embeds or [], view, author
        self.thread = None
        self.deleted = False
        self.guild = getattr(channel, "guild", None)

    @property
    def embed(self):
        return self.embeds[0] if self.embeds else None

    @property
    def jump_url(self):
        return f"https://discord.com/channels/1/{getattr(self.channel, 'id', 0)}/{self.id}"

    async def edit(self, *, content=None, embed=None, embeds=None, view=None, **kw):
        if embed is not None:
            self.embeds = [embed]
        if embeds is not None:
            self.embeds = embeds
        if content is not None:
            self.content = content
        self.view = view
        return self

    async def delete(self):
        self.deleted = True

    async def create_thread(self, *, name, **kw):
        t = Thread(Message._next + 5000, getattr(self.channel, 'guild', None), name)
        self.thread = t
        return t


class TextChannel:
    def __init__(self, id, guild=None, name="canal"):
        self.id, self.guild, self.name = id, guild, name
        self.sent = []
        self.fail_send = False
        self.category = None
        self.deleted = False

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def delete(self, **kw):
        self.deleted = True
        if self.guild is not None:
            self.guild._channels.pop(self.id, None)

    async def history(self, *, limit=100, oldest_first=False):
        msgs = [m for m in self.sent if not m.deleted]
        for m in (msgs if oldest_first else reversed(msgs))[:limit]:
            yield m

    async def send(self, content=None, *, embed=None, embeds=None, view=None, **kw):
        if self.fail_send:
            raise HTTPException("boom")
        msg = Message(self, content or "", ([embed] if embed else []) + list(embeds or []), view, author=getattr(self.guild, "me", None))
        self.sent.append(msg)
        return msg

    def permissions_for(self, who):
        return Permissions(view_channel=True, send_messages=True, embed_links=True)

    async def fetch_message(self, mid):
        for m in self.sent:
            if m.id == mid and not m.deleted:
                return m
        raise NotFound()


class VoiceChannel(TextChannel):
    async def edit(self, *, name=None, reason=None, **kw):
        if getattr(self, "fail_edit", None):
            raise self.fail_edit
        self.edits = getattr(self, "edits", []) + [name]
        if name is not None:
            self.name = name


class CategoryChannel(TextChannel):
    pass


class Thread(TextChannel):
    pass


class Interaction:
    pass


class Emoji:
    def __init__(self, id, name="wuju"):
        self.id, self.name = id, name

    def __str__(self):
        return f"<:{self.name}:{self.id}>"


def _escape_markdown(text, **kw):
    return text


# ---------------- discord.ext.commands ----------------
class Command:
    def __init__(self, callback, name=None, **kw):
        self.callback = callback
        self.name = name or callback.__name__
        self.__doc__ = callback.__doc__


def _command(name=None, **kw):
    def deco(fn):
        return Command(fn, name or fn.__name__)
    return deco


def _listener(*a, **kw):
    def deco(fn):
        fn.__is_listener__ = True
        return fn
    if a and callable(a[0]):
        return deco(a[0])
    return deco


class Cog:
    pass


class Bot:
    def __init__(self, *a, **kw):
        self.cogs = {}
        self.emojis = {}
        self.user = User(999, "bot", bot=True)
        self.persistent_views = []
        self.tree = types.SimpleNamespace(interaction_check=None, on_error=self._default_tree_error)
        self.checks = []

    async def _default_tree_error(self, interaction, error):
        self.tree_errors = getattr(self, "tree_errors", []) + [error]

    def add_check(self, fn):
        self.checks.append(fn)

    def get_emoji(self, eid):
        return self.emojis.get(eid)

    def get_cog(self, name):
        return self.cogs.get(name)

    async def add_cog(self, cog, **kw):
        self.cogs[type(cog).__name__] = cog
        if hasattr(cog, "cog_load"):
            await cog.cog_load()

    def add_view(self, view, **kw):
        self.persistent_views.append(view)

    def get_channel(self, cid):
        return getattr(self, "channels", {}).get(cid)

    async def fetch_channel(self, cid):
        ch = self.get_channel(cid)
        if ch is None:
            raise NotFound()
        return ch

    def get_guild(self, gid):
        return getattr(self, "guilds_map", {}).get(gid)

    async def wait_until_ready(self):
        pass


class Context:
    pass


class _Loop:
    def __init__(self, fn, **kw):
        self.fn, self.kw = fn, kw
        self.started = False

    def start(self, *a, **k):
        self.started = True

    def cancel(self):
        self.started = False

    def is_running(self):
        return self.started

    def before_loop(self, fn):
        self._before = fn
        return fn

    def __get__(self, obj, objtype=None):
        return self


def _loop(**kw):
    return lambda fn: _Loop(fn, **kw)


# ---------------- app_commands ----------------
class Choice:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def __class_getitem__(cls, item):
        return cls


class _Range:
    def __class_getitem__(cls, item):
        return float


class AppCommand:
    def __init__(self, callback, name, description="", parent=None):
        self.callback, self.name, self.description, self.parent = callback, name, description, parent
        self.checks = []
        self.choices_map = {}
        self.autocompletes = {}

    def autocomplete(self, param):
        def deco(fn):
            self.autocompletes[param] = fn
            return fn
        return deco

    def error(self, fn):
        self.on_error = fn
        return fn


def _app_command(*, name=None, description="", **kw):
    def deco(fn):
        return AppCommand(fn, name or fn.__name__, description)
    return deco


def _passthrough_decorator(*a, **kw):
    def deco(obj):
        return obj
    return deco


def _choices(**kw):
    def deco(obj):
        if isinstance(obj, AppCommand):
            obj.choices_map.update(kw)
        else:
            obj.__choices__ = kw
        return obj
    return deco


class Group:
    def __init__(self, *, name, description="", parent=None, **kw):
        self.name, self.description, self.parent = name, description, parent
        self.commands = {}
        if parent is not None:                      # como discord.py: un subgrupo cuelga de su grupo padre
            parent.commands[name] = self

    def command(self, *, name=None, description="", **kw):
        def deco(fn):
            cmd = AppCommand(fn, name or fn.__name__, description, parent=self)
            self.commands[cmd.name] = cmd
            return cmd
        return deco


# ---------------- ui ----------------
class Item:
    row = None


class Button(Item):
    def __init__(self, *, label=None, style=ButtonStyle.secondary, custom_id=None, disabled=False, emoji=None, row=None):
        self.label, self.style, self.custom_id, self.disabled, self.emoji, self.row = label, style, custom_id, disabled, emoji, row
        self.callback = None


class Select(Item):
    def __init__(self, *, placeholder=None, options=None, min_values=1, max_values=1, custom_id=None, disabled=False, row=None, **kw):
        self.placeholder, self.options = placeholder, options or []
        self.min_values, self.max_values, self.custom_id, self.disabled, self.row = min_values, max_values, custom_id, disabled, row
        self.values = []
        self.callback = None


class RoleSelect(Select):
    def __init__(self, *, placeholder=None, min_values=1, max_values=1, custom_id=None, disabled=False, row=None, default_values=None, **kw):
        super().__init__(placeholder=placeholder, min_values=min_values, max_values=max_values, custom_id=custom_id, disabled=disabled, row=row)
        self.default_values = default_values or []


class UserSelect(RoleSelect):
    pass


class ChannelSelect(RoleSelect):
    def __init__(self, *, channel_types=None, **kw):
        super().__init__(**kw)
        self.channel_types = channel_types or []


class ChannelType:
    text = "text"
    news = "news"
    forum = "forum"


class TextInput(Item):
    def __init__(self, *, label, style=TextStyle.short, placeholder=None, default=None, required=True, min_length=None, max_length=None, custom_id=None):
        self.label, self.style, self.placeholder, self.default = label, style, placeholder, default
        self.required, self.min_length, self.max_length = required, min_length, max_length
        self.value = default or ""


class View:
    def __init_subclass__(cls, **kw):
        super().__init_subclass__()
        cls.__item_defs__ = []
        for base in cls.__mro__[::-1]:
            for attr, value in vars(base).items():
                if getattr(value, "__discord_ui_button__", None) is not None:
                    cls.__item_defs__.append((attr, value))

    def __init__(self, *, timeout=180.0):
        self.timeout = timeout
        self.children = []
        for attr, func in getattr(type(self), "__item_defs__", []):
            kind, kwargs = func.__discord_ui_button__
            item = Button(**kwargs) if kind == "button" else Select(**kwargs)
            item.callback = (lambda inter, f=func, s=self, it=item: f(s, inter, it))
            setattr(self, attr, item)
            self.children.append(item)

    def add_item(self, item):
        self.children.append(item)
        return self

    def remove_item(self, item):
        if item in self.children:
            self.children.remove(item)
        return self

    def clear_items(self):
        self.children = []
        return self

    def stop(self):
        self.stopped = True

    async def on_timeout(self):
        pass


def _button(**kwargs):
    def deco(fn):
        fn.__discord_ui_button__ = ("button", kwargs)
        return fn
    return deco


def _select(**kwargs):
    def deco(fn):
        fn.__discord_ui_button__ = ("select", kwargs)
        return fn
    return deco


class Modal:
    def __init_subclass__(cls, *, title=None, **kw):
        super().__init_subclass__()
        cls.title = title

    def __init__(self, *, title=None, timeout=None, custom_id=None):
        if title:
            self.title = title
        self.children = []
        for name, value in list(vars(type(self)).items()):
            if isinstance(value, TextInput):
                import copy
                setattr(self, name, copy.copy(value))

    def add_item(self, item):
        self.children.append(item)


def install():
    d = types.ModuleType("discord")
    d.NotFound, d.Forbidden, d.HTTPException = NotFound, Forbidden, HTTPException
    d.Color, d.Embed, d.AllowedMentions, d.Object = Color, Embed, AllowedMentions, Object
    d.ButtonStyle, d.TextStyle, d.SelectOption, d.Permissions = ButtonStyle, TextStyle, SelectOption, Permissions
    d.Role, d.User, d.Member, d.Guild, d.Message = Role, User, Member, Guild, Message
    d.PermissionOverwrite = PermissionOverwrite
    d.TextChannel, d.Thread, d.Interaction, d.Emoji = TextChannel, Thread, Interaction, Emoji
    d.VoiceChannel, d.CategoryChannel = VoiceChannel, CategoryChannel
    d.ForumChannel = type('ForumChannel', (TextChannel,), {})
    d.Attachment = type('Attachment', (), {})

    class File:
        def __init__(self, fp, filename=None, **kw):
            self.fp, self.filename = fp, filename

        def read_text(self):
            self.fp.seek(0)
            return self.fp.read().decode("utf-8")
    d.File = File
    d.abc = types.SimpleNamespace(User=User, Messageable=TextChannel, GuildChannel=TextChannel)
    d.utils = types.SimpleNamespace(escape_markdown=_escape_markdown)
    class PartialEmoji:
        def __init__(self, name="", id=None):
            self.name, self.id = name, id
        @classmethod
        def from_str(cls, text):
            import re
            m = re.match(r"^<a?:(\w+):(\d+)>$", text.strip())
            return cls(m.group(1), int(m.group(2))) if m else cls(text.strip())
        def __str__(self):
            return f"<:{self.name}:{self.id}>" if self.id else self.name
    class RawReactionActionEvent:
        def __init__(self, guild_id, message_id, user_id, emoji):
            self.guild_id, self.message_id, self.user_id, self.emoji = guild_id, message_id, user_id, emoji
    class RawMessageDeleteEvent:
        def __init__(self, message_id):
            self.message_id = message_id
    d.PartialEmoji, d.RawReactionActionEvent, d.RawMessageDeleteEvent = PartialEmoji, RawReactionActionEvent, RawMessageDeleteEvent
    d.__version__ = "stub"

    ui = types.ModuleType("discord.ui")
    ui.View, ui.Button, ui.Select, ui.RoleSelect, ui.Modal, ui.TextInput, ui.Item = View, Button, Select, RoleSelect, Modal, TextInput, Item
    ui.UserSelect, ui.ChannelSelect = UserSelect, ChannelSelect
    ui.button, ui.select = _button, _select
    d.ui = ui
    d.ChannelType = ChannelType

    ac = types.ModuleType("discord.app_commands")
    ac.command, ac.describe, ac.choices = _app_command, _passthrough_decorator, _choices
    ac.default_permissions, ac.guild_only, ac.rename = _passthrough_decorator, _passthrough_decorator, _passthrough_decorator
    ac.Choice, ac.Range, ac.Group, ac.AppCommand = Choice, _Range, Group, AppCommand
    ac.autocomplete = lambda **kw: (lambda f: f)
    ac.check = lambda predicate: (lambda obj: obj)
    ac.CheckFailure = type("CheckFailure", (Exception,), {})
    ac.AppCommandError = Exception
    d.app_commands = ac

    ext = types.ModuleType("discord.ext")
    cmds = types.ModuleType("discord.ext.commands")
    cmds.Cog, cmds.Bot, cmds.Context = Cog, Bot, Context
    cmds.command = _command
    cmds.Cog.listener = staticmethod(_listener)
    cmds.guild_only = _passthrough_decorator
    cmds.CommandNotFound = type("CommandNotFound", (Exception,), {})
    cmds.CheckFailure = type("CheckFailure", (Exception,), {})
    tsk = types.ModuleType("discord.ext.tasks")
    tsk.loop = _loop
    ext.commands, ext.tasks = cmds, tsk
    d.ext = ext

    for name, mod in {"discord": d, "discord.ui": ui, "discord.app_commands": ac,
                      "discord.ext": ext, "discord.ext.commands": cmds, "discord.ext.tasks": tsk}.items():
        sys.modules[name] = mod
    return d
