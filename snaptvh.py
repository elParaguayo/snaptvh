#!/usr/bin/env python
# Copyright (c) 2025 elParaguayo
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
import argparse
import asyncio
import cmd
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from configparser import ConfigParser
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

EPG = "/api/epg/events/grid"

CHANNEL = r"EXTINF.*?,(.*?)\n(http.*?)\n"

DN = asyncio.subprocess.DEVNULL


def needs_channels(func):
    def wrapper(*args, **kwargs):
        obj = args[0]
        if obj.channels is None:
            return
        else:
            func(*args, **kwargs)

    wrapper.__doc__ = func.__doc__

    return wrapper


class PlaybackStatus:
    Playing = "playing"
    Stopped = "stopped"
    Paused = "paused"


class SnapTVH:

    def __init__(
        self,
        server="localhost",
        port="9981",
        username="",
        password="",
        mpvargs="",
        mpvpipe="/tmp/mpvpipe",
        cachedir="~/.local/cache/snaptvh",
        command_port=2000,
    ):
        super(SnapTVH, self).__init__()
        self.host = f"http://{server}:{port}"
        if username:
            self.auth = HTTPBasicAuth(username, password)
        else:
            self.auth = None
        self.cachedir = cachedir
        self.check_cache()
        self.extra_args = shlex.split(mpvargs)
        self.pipe = mpvpipe
        self.pipepath = Path(self.pipe).expanduser()
        self.proc = None
        self._refresh_timer = None
        self.channels = {}
        self._get_channels()
        self.mpvreader = self.mpvwriter = None
        self.cmd_port = int(command_port)

        self._metadata = {}
        self._properties = {}
        self._properties["playbackStatus"] = PlaybackStatus.Stopped
        self._properties["loopStatus"] = "none"
        self._properties["shuffle"] = False
        self._properties["volume"] = 100
        self._properties["mute"] = False
        self._properties["rate"] = 1.0
        self._properties["position"] = 0

        self._properties["canGoNext"] = True
        self._properties["canGoPrevious"] = True
        self._properties["canPlay"] = True
        self._properties["canPause"] = True
        self._properties["canSeek"] = False
        self._properties["canControl"] = True

        self._last_played = ""

        self._cmd_server_ready = asyncio.Event()

        self._tasks = []

        self._stop_event = asyncio.Event()

    def check_cache(self):
        self.cache = Path(self.cachedir).expanduser()
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            raise ValueError(
                f"Unable to create cache folder at: {self.cache.as_posix()}"
            )

        self.channels_file = self.cache / "channels.conf"

    def queue(self, coroutine, done_callback=None):
        """
        Helper method to add a task to the loop.

        Where no callback is provided, a default no_op is used.
        """

        # Wrapper around queued tasks so we can cancel them
        # at exit
        def wrap(func):
            def _wrapped_task(task):
                func(task)
                self._tasks.remove(task)

            return _wrapped_task

        def no_op(_):
            pass

        task = asyncio.create_task(coroutine)
        self._tasks.append(task)
        task.add_done_callback(wrap(done_callback or no_op))

    def start(self):
        self._loop = asyncio.new_event_loop()
        for sig in [signal.SIGINT, signal.SIGTERM]:
            self._loop.add_signal_handler(sig, self._stop_event.set)
        self._loop.run_until_complete(self._start())

    async def _start(self):
        self.reader, self.writer = await self.connect_stdin_stdout()
        self.queue(self.start_cmd_server())
        await self._cmd_server_ready.wait()
        self.queue(self._read_stdin())
        await self.send(method="Plugin.Stream.Ready")
        await self._stop_event.wait()
        self._clear_timer()
        for task in self._tasks:
            task.cancel()

    async def connect_stdin_stdout(self):
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await self._loop.connect_read_pipe(lambda: protocol, sys.stdin)
        w_transport, w_protocol = await self._loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        writer = asyncio.StreamWriter(w_transport, w_protocol, reader, self._loop)
        return reader, writer

    async def _read_stdin(self):
        while not self._stop_event.is_set():
            res = await self.reader.readline()

            if not res:
                break

            await self.parse_json_cmd(res.strip().decode())

        if not self._stop_event.is_set():
            self._stop_event.set()

    async def start_cmd_server(self):
        self.cmd_server = await asyncio.start_server(
            self.handle_cmd_client, "0.0.0.0", self.cmd_port
        )
        self._cmd_server_ready.set()
        # await self.cmd_server.serve_forever()

    async def handle_cmd_client(self, reader, writer):
        await self.writer.drain()
        while not self._stop_event.is_set():
            data = await reader.readline()
            message = data.decode().strip()
            if not message:
                break
            command, _, args = message.partition(" ")
            loop = await self.parse_cmd(command, args, writer)
            if not loop:
                break

        writer.close()

    async def log(self, message, level="Info"):
        params = {"severity": level, "message": message}
        await self.send(method="Plugin.Stream.Log", params=params)

    async def set_mpv_property(self, prop, value):
        if self.mpvwriter is None:
            return False

        data = {"command": ["set_property", prop, value]}
        self.mpvwriter.write(f"{json.dumps(data)}\r\n".encode())
        await self.mpvwriter.drain()
        line = await self.mpvreader.readline()
        result = json.loads(line.decode().strip()).get("error", "")
        return result == "success"

    async def send(self, **kwargs):
        data = {"jsonrpc": "2.0"}
        data.update(kwargs)
        self.writer.write(f"{json.dumps(data)}\r\n".encode())
        await self.writer.drain()

    async def set_property(self, name, value, send_metadata=False):
        self._properties[name] = value
        await self.notify_properties(send_metadata=send_metadata)

    async def notify_properties(self, send_metadata=False):
        params = self._properties.copy()
        if send_metadata:
            params["metadata"] = self._metadata.copy()
        await self.send(method="Plugin.Stream.Player.Properties", params=params)

    def _get_channels(self):
        if not self.channels_file.is_file():
            self._download_channels()

        self.load_channels()

    def _download_channels(self):
        r = requests.get(f"{self.host}/playlist")
        with open(self.channels_file, "wb") as c_list:
            c_list.write(r.content)

    def search_epg(self, keyword):
        params = {"mode": "now", "fulltext": 1, "title": keyword}
        r = requests.get(f"{self.host}{EPG}", auth=self.auth, params=params)
        if r.status_code != 200:
            raise ValueError("Couldn't get EPG data.")

        return r.json().get("entries", False)

    def set_now_playing(self, channel):
        if not self.proc:
            return

        params = {"channel": channel, "mode": "now"}

        r = requests.get(f"{self.host}{EPG}", auth=self.auth, params=params)

        if r.status_code != 200:
            raise ValueError("Couldn't get Now Playing data.")

        epg = r.json().get("entries", False)

        if not epg:
            raise ValueError("No EPG data")

        prog = epg[0]
        self._metadata["title"] = prog["title"]
        self._metadata["artist"] = [channel]
        self.schedule_refresh(prog)

    def schedule_refresh(self, prog):
        now = time.time()
        delta = prog["stop"] - now
        delta = max(delta, 0) + 5
        self._refresh_timer = self._loop.call_later(delta, self.set_now_playing)

    async def _play_channel(self, channel):
        self._clear_timer()

        url = self._channel_map.get(channel.lower())
        if not url:
            return

        cmd = ["mpv"]
        cmd.extend(self.extra_args)
        cmd.append(f"--input-ipc-server={self.pipe}")
        cmd.append(url)

        started = await self._start_mpv(cmd)

        self._last_played = channel
        self._metadata = {}
        try:
            self.set_now_playing(channel)
        except ValueError:
            self._metadata["title"] = channel
        await self.set_property(
            "playbackStatus", PlaybackStatus.Playing, send_metadata=True
        )

    async def is_running(self):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.proc.wait(), 1e-6)
        return self.proc.returncode is None

    async def _start_mpv(self, cmd):
        if self.proc:
            await self.stop()

        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=DN, stdout=DN, stderr=DN
        )
        while await self.is_running():
            if not self.pipepath.exists():
                await asyncio.sleep(0.01)
                continue

            try:
                self.mpvreader, self.mpvwriter = await asyncio.open_unix_connection(
                    path=self.pipe
                )
            except ConnectionRefusedError:
                await asyncio.sleep(0.1)
                continue

            self.queue(self._listen_mpv_exit())

            return True

        self.mpvreader = self.mpvwriter = None
        return False

    async def _listen_mpv_exit(self):
        await self.proc.wait()
        await self.set_property("playbackStatus", PlaybackStatus.Stopped)
        self.proc = None

    def load_channels(self):
        if not self.channels_file.is_file():
            self.channels = None
            return
        with open(self.channels_file, "r") as raw:
            data = raw.read()

        channels = re.findall(CHANNEL, data)

        self._channel_map = {c.lower(): l for c, l in channels}
        self.channels = [x for x, _ in channels]
        self.channels.sort()

    # @needs_channels
    async def play(self, channel_name):
        """Play named channel."""
        if channel_name.lower() not in self._channel_map:
            return

        channel = [c for c in self.channels if channel_name.lower() == c.lower()][0]

        await self._play_channel(channel)

    async def stop(self):
        """Stops the playing channel."""
        if self.proc:
            self.proc.terminate()
            self.proc = None

        if self.mpvwriter is not None:
            self.mpvwriter.close()
            await self.mpvwriter.wait_closed()
            self.mpvreader = self.mpvwriter = None

        self._clear_timer()

        await self.set_property("playbackStatus", PlaybackStatus.Stopped)

    def _clear_timer(self):
        if self._refresh_timer is not None:
            self._refresh_timer.cancel()
            self._refresh_timer = None

    async def parse_cmd(self, command, args, writer):
        match command.lower():
            case "list":
                if args:
                    channels = [
                        c for c in self.channels if c.lower().startswith(args.lower())
                    ]
                else:
                    channels = self.channels
                writer.write(f'{"\n".join(channels)}\n'.encode())
            case "status":
                if self.proc:
                    writer.write(f"Playing: {self._last_played}\n".encode())
                else:
                    writer.write("Not playing.\n".encode())
            case "pause":
                await self.stop()
            case "stop":
                await self.stop()
            case "play":
                if args:
                    await self.play(args)
                else:
                    await self.last_played()
            case "quit":
                return False
            case "search":
                if not args:
                    return True

                try:
                    entries = self.search_epg(args)
                except ValueError as e:
                    writer.write(f"{e}\n".encode())
                    return True

                for entry in entries:
                    writer.write(
                        "{channelName}\n{title}\n{subtitle}\n\n".format(
                            **entry
                        ).encode()
                    )
            case _:
                pass

        return True

    async def parse_json_cmd(self, line):
        try:
            data = json.loads(line.strip())
        except json.JSONDecodeError:
            return line

        msg_id = data["id"]
        method = data["method"]
        params = data.get("params", dict())
        prefix, command = method.rsplit(".", 1)

        if prefix == "Plugin.Stream.Player":
            match command:
                case "Control":
                    match params["command"]:
                        case "play":
                            await self.last_played()
                        case "pause":
                            await self.stop()
                        case "playPause":
                            pass
                        case "stop":
                            await self.stop()
                        case "next":
                            self.next()
                        case "previous":
                            self.previous()
                        # Custom method
                        case "playChannel":
                            self._play_channel(params["params"]["channel"])

                    await self.send(result="ok", id=msg_id)

                case "SetProperty":
                    for prop, value in params.items():
                        match prop:
                            case "loopStatus":
                                # loopStatus: [string] the current repeat status, one of:
                                # none: the playback will stop when there are no more tracks to play
                                # track: the current track will start again from the begining once it has finished playing
                                # playlist: the playback loops through a list of tracks
                                pass
                            case "shuffle":
                                # [bool] play playlist in random order
                                pass
                            case "volume":
                                # [int] voume in percent, valid range [0..100]
                                success = await self.set_mpv_property(
                                    "ao-volume", value
                                )
                                if success:
                                    self._properties["volume"] = value
                            case "mute":
                                # [bool] the current mute state
                                success = await self.set_mpv_property(
                                    "ao-mute", int(value)
                                )
                                if succss:
                                    self._properties["mute"] = value
                            case "rate":
                                # rate: [float] the current playback rate, valid range (0..)
                                pass

                case "GetProperties":
                    await self.send(id=msg_id, result=self._properties)

    async def last_played(self):
        if self._last_played:
            await self.play(self._last_played)


if __name__ == "__main__":
    cp = ConfigParser()

    if cp.read(
        [Path("/etc/snaptvh"), Path("~/.config/snaptvh/config.ini").expanduser()]
    ):
        settings = cp["settings"]
    else:
        settings = {}

    tv = SnapTVH(**settings)
    tv.start()
