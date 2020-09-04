# The MIT License (MIT)
#
# Copyright (C) 2019 - Jérémie Galarneau <jeremie.galarneau@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import socket
import sys
import signal
import threading
import time
import json
import re
import tempfile
import glob
import git
import irc
import irc.bot
def _format_commit(
    commit,
    before_hash='',
    after_hash='',
    before_summary='',
    after_summary='',
    before_author='',
    after_author='',
):
    hash = f'{before_hash}{commit.hexsha[:8]}{after_hash}'
    summary = f'{before_summary}{commit.summary}{after_summary}'
    author = f'{before_author}{commit.author.name}{after_author}'
    return f'{hash} {summary} [{author}]'


class _Repository:
    def __init__(self, name, url, last_seen_commit_sha=None):
        self._name = name
        self._url = url
        print(f'Cloning {self._name}')
        self._directory = tempfile.TemporaryDirectory()
        git.Git(self._directory.name).clone(self._url)
        self._repo = git.Repo(glob.glob(f'{self._directory.name}/*/')[0])

        if last_seen_commit_sha is not None:
            self._last_seen_commit = self._repo.commit(last_seen_commit_sha)
        else:
            self._last_seen_commit = next(self._commit_iter)

    @property
    def _commit_iter(self):
        return self._repo.iter_commits('origin/master')

    @property
    def name(self):
        return self._name

    def get_new_commits(self):
        try:
            self._repo.remotes.origin.fetch()
        except (git.exc.GitCommandError, git.exc.BadName):
            # Typically, this means the host could not be resolved;
            # return an empty list and try again later.
            return []

        new_commits = []

        for commit in self._commit_iter:
            if commit == self._last_seen_commit:
                break

            new_commits.append(commit)

        if len(new_commits) > 0:
            self._last_seen_commit = new_commits[0]

        return new_commits


class _IrcBot(irc.bot.SingleServerIRCBot):
    def __init__(self, channel_name, nick, server, port=6667):
        super().__init__([irc.bot.ServerSpec(server, port)], nick, nick)
        self._channel_name = channel_name
        self._connection = None

    def on_nicknameinuse(self, connection, _):
        connection.nick(f'{connection.get_nickname()}_')

    def on_welcome(self, connection, _):
        self._connection = connection
        connection.join(self._channel_name)

    def msg_channel(self, msg):
        if self._connection is None:
            # not connected yet
            return

        self._connection.privmsg(self._channel_name, msg)

    def disconnect(self):
        if self._connection is None:
            # not connected yet
            return

        self._connection.disconnect()


def main():
    if len(sys.argv) != 2:
        print('Usage: combotcha config.json')
        sys.exit(1)

    cfg = None
    with open(sys.argv[1]) as cfg_file:
        cfg = json.load(cfg_file)

    irc_cfg = cfg['irc']
    irc_bot = _IrcBot(
        irc_cfg['channel'], irc_cfg['nick'], irc_cfg['url'], irc_cfg['port']
    )

    repos = []
    repos_cfg = cfg['repos']
    for repo_cfg in repos_cfg:
        repo = _Repository(
            repo_cfg['name'],
            repo_cfg['url'],
            repo_cfg.get('last_seen_commit_sha', None),
        )
        repos.append(repo)

    def sigint_handler(sig, frame):
        irc_bot.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, sigint_handler)

    print('Launching IRC thread')
    irc_thread = threading.Thread(target=irc_bot.start)
    irc_thread.start()

    while True:
        for repo in repos:
            new_commits = repo.get_new_commits()
            if not new_commits:
                continue

            print(
                '{} new commits found for {} repository'.format(
                    len(new_commits), repo.name
                )
            )
            irc_bot.msg_channel('{} ({})'.format(repo.name, len(new_commits)))
            rate_limit = False
            if len(new_commits) > 5:
                rate_limit = True
            for commit in new_commits:
                irc_bot.msg_channel(
                    _format_commit(
                        commit,
                        before_hash='\x0307',
                        after_hash='\x0f',
                        before_summary='\x0300',
                        after_summary='\x0f',
                        before_author='\x0303',
                        after_author='\x0f',
                    )
                )
                if rate_limit:
                    time.sleep(1)

        time.sleep(10)
