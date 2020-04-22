"""
The MIT License (MIT)

Copyright (c) 2017-2020 TwitchIO

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import asyncio
import importlib
import inspect
import itertools
import sys
import traceback
import warnings
from typing import Callable, Optional, Union, Dict
from twitchio.channel import Channel
from twitchio.client import Client
from twitchio.websocket import WSConnection
from .core import *
from .errors import *
from .meta import Cog
from .stringparser import StringParser
from .utils import _CaseInsensitiveDict


class Bot(Client):

    def __init__(self, irc_token: str, *, nick: str, prefix: Union[str, list, tuple],
                 initial_channels: Union[list, tuple, Callable] = None, **kwargs):
        super().__init__(client_id=kwargs.get('client_id'))

        self.loop = kwargs.get('loop', asyncio.get_event_loop())
        self._connection = WSConnection(bot=self, token=irc_token, nick=nick.lower(), loop=self.loop,
                                        initial_channels=initial_channels)

        self._prefix = prefix
        self._nick = nick.lower()

        if kwargs.get('case_insensitive', False):
            self._commands = _CaseInsensitiveDict()
            self._command_aliases = _CaseInsensitiveDict()
        else:
            self._commands = {}
            self._command_aliases = {}

        self._modules = {}
        self._cogs = {}
        self._events: Dict[list] = {}
        self._checks = []

        self.__init__commands__()

    def __init__commands__(self):
        commands = inspect.getmembers(self)

        for _, obj in commands:
            if not isinstance(obj, Command):
                continue

            obj._instance = self

            try:
                self.add_command(obj)
            except TwitchCommandError:
                traceback.print_exc()
                continue

    async def __get_prefixes__(self, message):
        ret = self._prefix

        if callable(self._prefix):
            if inspect.iscoroutinefunction(self._prefix):
                ret = await self._prefix(self, message)
            else:
                ret = self._prefix(self, message)

        if not isinstance(ret, (list, tuple, set, str)):
            raise TypeError(f'Prefix must be of either class <list, tuple, set, str> not <{type(ret)}>')

        return ret

    async def get_prefix(self, message):
        # TODO Docs
        prefixes = await self.__get_prefixes__(message)

        if not isinstance(prefixes, str):
            for prefix in prefixes:
                if message.content.startswith(prefix):
                    return prefix
        elif message.content.startswith(prefixes):
            return prefixes
        else:
            return None

    def add_command(self, command: Command):
        """Method which registers a command for use by the bot.

        Parameters
        ------------
        command: :class:`.Command`
            The command to register.
        """
        if not isinstance(command, Command):
            raise TypeError('Commands passed must be a subclass of Command.')
        elif command.name in self.commands:
            raise TwitchCommandError(f'Failed to load command <{command.name}>, a command with that name already exists.')
        elif not inspect.iscoroutinefunction(command._callback):
            raise TwitchCommandError(f'Failed to load command <{command.name}>. Commands must be coroutines.')

        self.commands[command.name] = command

        if not command.aliases:
            return

        for alias in command.aliases:
            if alias in self.commands:
                del self.commands[command.name]
                raise TwitchCommandError(
                    f'Failed to load command <{command.name}>, a command with that name/alias already exists.')

            self._command_aliases[alias] = command.name

    def get_command(self, name: str) -> Optional[Command]:
        """Method which retrieves a registered command.

        Parameters
        ------------
        name: str
            The name or alias of the command to retrieve.

        Returns
        ---------
        Optional[:class:`.Command`]
        """
        name = self._command_aliases.get(name, name)

        return self._commands.get(name, None)

    async def get_context(self, message, *, cls=None):
        # TODO Docs
        if not cls:
            cls = Context

        prefix = await self.get_prefix(message)
        if not prefix:
            return cls(message=message, prefix=prefix, valid=False)

        content = message.content[len(prefix)::].lstrip(' ')  # Strip prefix and remainder whitespace
        parsed = StringParser().process_string(content)  # Return the string as a dict view

        try:
            command_ = parsed.pop(0)
        except KeyError:
            raise CommandNotFound(f'No valid command was passed.')

        try:
            command_ = self._command_aliases[command_]
        except KeyError:
            pass

        if command_ in self.commands:
            command_ = self.commands[command_]
        else:
            raise CommandNotFound(f'No command "{command_}" was found.')

        args, kwargs = command_.parse_args(command_._instance, parsed)

        context = cls(message=message, prefix=prefix, command=command_, args=args, kwargs=kwargs,
                      valid=True)
        return context

    async def handle_commands(self, message):
        context = await self.get_context(message)

        await self.invoke(context)

    async def invoke(self, context):
        # TODO Docs
        if not context.prefix:
            return

        async def try_run(func, *, to_command=False):
            try:
                await func
            except Exception as _e:
                if not to_command:
                    self.run_event("error", _e)
                else:
                    self.run_event("command_error", context, _e)

        check_result = await self.handle_checks(context)

        if check_result is not True:
            self.run_event("command_error", context, check_result)
            return

        limited = self._run_cooldowns(context)

        if limited:
            self.run_event("command_error", context, limited[0])
            return

        instance = context.command._instance
        if instance:
            args = [instance, context]
        else:
            args = [context]

        await try_run(self.global_before_invoke(context))

        if context.command._before_invoke:
            await try_run(context.command._before_invoke(*args), to_command=True)

        try:
            await context.command._callback(*args, *context.args, **context.kwargs)

        except Exception as e:
            if context.command.event_error:
                await try_run(context.command.event_error(*args, e))

            self.run_event("command_error", context, e)

        # Invoke our after command hooks...
        if context.command._after_invoke:
            await try_run(context.command._after_invoke(*args), to_command=True)

        await try_run(self.global_after_invoke(context))

    def _run_cooldowns(self, context):
        try:
            buckets = context.command._cooldowns[0].get_buckets(context)
        except IndexError:
            return None

        expired = []

        try:
            for bucket in buckets:
                bucket.update_bucket(context)
        except CommandOnCooldown as e:
            expired.append(e)

        return expired

    async def handle_checks(self, context):
        # TODO Docs
        command_ = context.command

        if not command_.no_global_checks:
            checks = [predicate for predicate in itertools.chain(self._checks, command_._checks)]
        else:
            checks = command_._checks

        if not checks:
            return True

        try:
            for predicate in checks:
                if inspect.isawaitable(predicate):
                    result = await predicate(context)
                else:
                    result = predicate(context)

                if result is False:
                    raise CheckFailure(f'The check <{predicate}> for command <{command_.name}> failed.')

            return True
        except Exception as e:
            return e

    def load_module(self, name: str):
        """Method which loads a module and it's cogs.

        Parameters
        ------------
        name: str
            The name of the module to load in dot.path format.
        """
        if name in self._modules:
            raise ValueError(f"Module <{name}> is already loaded")

        module = importlib.import_module(name)

        if hasattr(module, 'prepare'):
            module.prepare(self)
        else:
            del module
            del sys.modules[name]
            raise ImportError(f'Module <{name}> is missing a prepare method')

        self._modules[name] = module

    def unload_module(self, name: str):
        if name not in self._modules:
            raise ValueError(f"Module <{name}> is not loaded")

        module = self._modules.pop(name)
        if hasattr(module, "breakdown"):
            module.breakdown(self)

        to_delete = [cog_name for cog_name, cog in self._cogs.items() if cog.__module__ == module]
        for name in to_delete:
            self.remove_cog(name)

        # just in case they aren't in cogs
        to_delete = [name for name, cmd in self._commands if cmd._callback.__module__ == module]
        for name in to_delete:
            del self._commands[name]

        to_delete = [name for name, cmd in self._command_aliases if cmd._callback.__module__ == module]
        for name in to_delete:
            del self._command_aliases[name]

    def reload_module(self, name: str):
        if name not in self._modules:
            raise ValueError(f"Module <{name}> is not loaded")

        commands = {}
        aliases = {}
        cogs = {}

        module = self._modules.pop(name)
        to_pop = []
        for name, cog in self._cogs:
            if cog.__module__ == module:
                cogs[name] = cog
            to_pop.append(name)

        for name in to_pop:
            del self._cogs[name]

        # remove commands
        to_delete = [(name, cmd) for name, cmd in self._commands if cmd._callback.__module__ == module]
        for name, cmd in to_delete:
            commands[name] = cmd
            del self._commands[name]

        to_delete = [(name, cmd) for name, cmd in self._command_aliases if cmd._callback.__module__ == module]
        for name, cmd in to_delete:
            aliases[name] = cmd
            del self._command_aliases[name]

        del sys.modules[name]
        try:
            self.load_module(name)
        except:
            sys.modules[name] = module
            self._modules[name] = module
            self._command_aliases.update(aliases)
            self._commands.update(commands)
            self._cogs.update(cogs)
            raise

    def add_cog(self, cog):
        if not isinstance(cog, Cog):
            raise InvalidCog('Cogs must derive from "commands.Cog".')

        if cog.name in self._cogs:
            raise InvalidCog(f'Cog "{cog.name}" has already been loaded.')

        cog._load_methods(self)
        self._cogs[cog.name] = cog

    def remove_cog(self, cog_name: str):
        if cog_name not in self._cogs:
            raise InvalidCog(f"Cog '{cog_name}' not found")

        cog = self._cogs.pop(cog_name)
        to_remove = [name for name, cmd in self._commands.items() if cmd.cog is not None and cmd.cog.name == cog.name]
        for name in to_remove:
            del self._commands[name]

        to_remove = [name for name, cmd in self._command_aliases if cmd.cog is not None and cmd.cog.name == cog.name]
        for name in to_remove:
            del self._command_aliases[name]

    async def global_before_invoke(self, ctx):
        """|coro|

        Method which is called before any command is about to be invoked.

        This method is useful for setting up things before command invocation. E.g Database connections or
        retrieving tokens for use in the command.

        Parameters
        ------------
        ctx:
            The context used for command invocation.

        Examples
        ----------
        .. code:: py

            async def global_before_invoke(self, ctx):
                # Make a database query for example to retrieve a specific token.
                token = db_query()

                ctx.token = token

            async def my_command(self, ctx):
                data = await self.create_clip(ctx.token, ...)

        Note
        ------
            The global_before_invoke is called before any other command specific hooks.
        """
        pass

    async def global_after_invoke(self, ctx):
        """|coro|

        Method which is called after any command is invoked regardless if it failed or not.

        This method is useful for cleaning up things after command invocation. E.g Database connections.

        Parameters
        ------------
        ctx:
            The context used for command invocation.

        Note
        ------
            The global_after_invoke is called only after the command successfully invokes.
        """
        pass

    def run_event(self, name, *args, **kwargs):
        async def wrapped(func):
            try:
                await func(*args, **kwargs)
            except Exception as e:
                self.run_event("error", e)

        inner_cb = getattr(self, name, None)
        if inner_cb is not None:
            if inspect.iscoroutine(inner_cb):
                self.loop.create_task(wrapped(inner_cb))
            else:
                warnings.warn(f"event '{name}' callback is not a coroutine", category=RuntimeWarning)

        if name in self._events:
            for event in self._events[name]:
                self.loop.create_task(wrapped(event))

    def event(self, name: str = None) -> Callable:

        def decorator(func: Callable) -> Callable:
            self.add_event(func, name)
            return func

        return decorator

    def add_event(self, callback: Callable, name: str = None) -> None:
        if not inspect.iscoroutine(callback):
            raise ValueError("callback must be a coroutine")

        event_name = name or callback.__name__
        callback._event = event_name  # used to remove the event

        if event_name in self._events:
            self._events[event_name].append(callback)

        else:
            self._events[event_name] = [callback]

    def remove_event(self, callback: Callable) -> bool:
        if not hasattr(callback, "_event"):
            raise ValueError("callback is not a registered event")

        if callback in self._events[callback._event]:
            self._events[callback._event].remove(callback)
            return True

        return False

    @property
    def nick(self):
        return self._nick

    @property
    def commands(self):
        return self._commands

    @property
    def events(self):
        return self._events

    @property
    def cogs(self):
        return self._cogs

    def get_channel(self, name: str):
        """Retrieve a channel from the cache.

        Parameters
        -----------
        name: str
            The channel name to retrieve from cache. Returns None if no channel was found.

        Returns
        --------
            :class:`.Channel`
        """
        name = name.lower()

        try:
            self._connection._cache[name]  # this is a bit silly, but for now it'll do...
        except KeyError:
            return None

        # Basically the cache doesn't store channels naturally, instead it stores a channel key
        # With the associated users as a set.
        # We create a Channel here and return it only if the cache has that channel key.

        channel = Channel(name=name, websocket=self._connection, bot=self)
        return channel

    async def event_command_error(self, context, error):
        """|coro|

        Event called when an error occurs during command invocation.

        Parameters
        ------------
        context: :class:`.Context`
            The command context.
        error: :class:`.Exception`
            The exception raised while trying to invoke the command.
        """
        print(f'Ignoring exception in command: {error}:', file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    async def event_mode(self, channel, user, status):
        """|coro|

        Event called when a MODE is received from Twitch.

        Parameters
        ------------
        channel: :class:`.Channel`
            Channel object relevant to the MODE event.
        user: :class:`.User`
            User object containing relevant information to the MODE.
        status: str
            The JTV status received by Twitch. Could be either o+ or o-.
            Indicates a moderation promotion/demotion to the :class:`.User`
        """
        pass

    async def event_userstate(self, user):
        """|coro|

        Event called when a USERSTATE is received from Twitch.

        Parameters
        ------------
        user: :class:`.User`
            User object containing relevant information to the USERSTATE.
        """
        pass

    async def event_raw_usernotice(self, channel, tags: dict):
        """|coro|

        Event called when a USERNOTICE is received from Twitch.
        Since USERNOTICE's can be fairly complex and vary, the following sub-events are available:

            :meth:`event_usernotice_subscription` :
            Called when a USERNOTICE Subscription or Re-subscription event is received.


        .. seealso::

            For more information on how to handle USERNOTICE's visit:
            https://dev.twitch.tv/docs/irc/tags/#usernotice-twitch-tags


        Parameters
        ------------
        channel: :class:`.Channel`
            Channel object relevant to the USERNOTICE event.
        tags : dict
            A dictionary with the relevant information associated with the USERNOTICE.
            This could vary depending on the event.
        """
        pass

    async def event_usernotice_subscription(self, metadata):
        """|coro|

        Event called when a USERNOTICE subscription or re-subscription event is received from Twitch.

        Parameters
        ------------
        metadata: :class:`twitchio.dataclasses.NoticeSubscription`
            The object containing various metadata about the subscription event.
            For ease of use, this contains a :class:`.User` and :class:`.Channel`.
        """
        pass

    async def event_part(self, user):
        """|coro|

        Event called when a PART is received from Twitch.

        Parameters
        ------------
        user: :class:`.User`
            User object containing relevant information to the PART.
        """
        pass

    async def event_join(self, channel, user):
        """|coro|

        Event called when a JOIN is received from Twitch.

        Parameters
        ------------
        channel: :class:`.Channel`
            The channel associated with the JOIN.
        user: :class:`.User`
            User object containing relevant information to the JOIN.
        """
        pass

    async def event_message(self, message):
        """|coro|

        Event called when a PRIVMSG is received from Twitch.

        Parameters
        ------------
        message: :class:`.Message`
            Message object containing relevant information.
        """
        await self.handle_commands(message)

    async def event_error(self, error: Exception, data=None):
        """|coro|

        Event called when an error occurs while processing data.

        Parameters
        ------------
        error: Exception
            The exception raised.
        data: str
            The raw data received from Twitch. Depending on how this is called, this could be None.

        Example
        ---------
        .. code:: py

            @bot.event
            async def event_error(error, data):
                traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
        """
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    async def event_ready(self):
        """|coro|

        Event called when the Bot has logged in and is ready.

        Example
        ---------
        .. code:: py

            @bot.event
            async def event_ready():
                print(f'Logged into Twitch | {bot.nick}')
        """
        pass

    async def event_raw_data(self, data):
        """|coro|

        Event called with the raw data received by Twitch.

        Parameters
        ------------
        data: str
            The raw data received from Twitch.

        Example
        ---------
        .. code:: py

            @bot.event
            async def event_raw_data(data):
                print(data)
        """
        pass

    def command(self, *, name: str=None, aliases: Union[list, tuple]=None, cls=Command, no_global_checks=False):
        """Decorator which registers a command with the bot.

        Commands must be a coroutine.

        Parameters
        ------------
        name: str [Optional]
            The name of the command. By default if this is not supplied, the function name will be used.
        aliases: Optional[Union[list, tuple]]
            The command aliases. This must be a list or tuple.
        cls: class [Optional]
            The custom command class to override the default class. This must be similar to :class:`.Command`.
        no_global_checks : Optional[bool]
            Whether or not the command should abide by global checks. Defaults to False, which checks global checks.

        Raises
        --------
        TypeError
            cls is not type class.
        """

        if not inspect.isclass(cls):
            raise TypeError(f'cls must be of type <class> not <{type(cls)}>')

        def decorator(func):
            cmd_name = name or func.__name__

            cmd = cls(name=cmd_name, func=func, aliases=aliases, instance=None)
            self.add_command(cmd)

            return cmd
        return decorator

    def run(self):
        try:
            self.loop.create_task(self._connection._connect())
            self.loop.run_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.loop.create_task(self.close())

    async def close(self):
        # TODO session close
        self._connection._close()
