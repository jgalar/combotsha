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
import logging
import os


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
        self._logger = (
            logging.getLogger(__name__).getChild(self.__class__.__name__).getChild(name)
        )
        self._logger.info('Creating repository object.')
        self._name = name
        self._url = url
        self._directory = tempfile.TemporaryDirectory()
        self._logger.info(
            f'Cloning Git repository `{url}` within `{self._directory.name}`.'
        )
        git.Git(self._directory.name).clone(self._url)
        self._repo = git.Repo(glob.glob(f'{self._directory.name}/*/')[0])

        if last_seen_commit_sha is not None:
            self._last_seen_commit = self._repo.commit(last_seen_commit_sha)
        else:
            self._last_seen_commit = next(self._commit_iter)

        self._logger.info(
            f'Last seen commit is: {_format_commit(self._last_seen_commit)}.'
        )

    @property
    def _commit_iter(self):
        return self._repo.iter_commits('origin/master')

    @property
    def name(self):
        return self._name

    def get_new_commits(self):
        self._logger.debug('Fetching new commits.')

        try:
            self._repo.remotes.origin.fetch()
        except (git.exc.GitCommandError, git.exc.BadName) as exc:
            # Typically, this means the host could not be resolved;
            # return an empty list and try again later.
            self._logger.error(f'Git error: {exc}')
            return []

        new_commits = []

        for commit in self._commit_iter:
            if commit == self._last_seen_commit:
                break

            new_commits.append(commit)

        self._logger.debug(f'Found {len(new_commits)} new commits.')

        if len(new_commits) > 0:
            self._last_seen_commit = new_commits[0]
            self._logger.info(
                f'New last seen commit is: {_format_commit(self._last_seen_commit)}.'
            )

        return new_commits


class _IrcBot(irc.bot.SingleServerIRCBot):
    def __init__(self, channel_name, nick, server, port=6667):
        super().__init__([irc.bot.ServerSpec(server, port)], nick, nick)
        self._logger = logging.getLogger(__name__).getChild(self.__class__.__name__)
        self._logger.info(f'Creating IRC bot to connect to `{server}:{port}`.')
        self._channel_name = channel_name
        self._connection = None

    def on_nicknameinuse(self, connection, _):
        new_nick = f'{connection.get_nickname()}_'
        self._logger.info(f'Nick in use: trying `{new_nick}`.')
        connection.nick(new_nick)

    def on_welcome(self, connection, _):
        self._logger.info(
            f'Connected to server: joining channel `{self._channel_name}`'
        )
        self._connection = connection
        connection.join(self._channel_name)

    def msg_channel(self, msg):
        if self._connection is None:
            # not connected yet
            return

        self._logger.info(f'Sending private message to channel `{self._channel_name}`.')
        self._connection.privmsg(self._channel_name, msg)

    def disconnect(self):
        if self._connection is None:
            # not connected yet
            return

        self._logger.info('Disconnecting.')
        self._connection.disconnect()


def _configure_logging():
    level = {
        'C': logging.CRITICAL,
        'E': logging.ERROR,
        'W': logging.WARNING,
        'I': logging.INFO,
        'D': logging.DEBUG,
        'N': logging.NOTSET,
    }[os.environ.get('COMBOTSHA_LOG_LEVEL', 'I')]
    logging.basicConfig(
        level=level, format='{asctime} [{levelname}] {name}: {message}', style='{'
    )


def _main():
    def fatal_error(msg):
        logger.setLevel(logging.CRITICAL)
        logger.critical(msg)
        sys.exit(1)

    def create_repos():
        cfg_repos = cfg['repos']
        repos = []

        for repo_cfg in cfg_repos:
            repos.append(
                _Repository(
                    repo_cfg['name'],
                    repo_cfg['url'],
                    repo_cfg.get('last_seen_commit_sha'),
                )
            )

        return repos

    def create_irc_bot():
        cfg_irc = cfg['irc']
        irc_bot = _IrcBot(
            cfg_irc['channel'], cfg_irc['nick'], cfg_irc['url'], cfg_irc['port']
        )
        logger.info('Starting IRC bot thread.')
        irc_thread = threading.Thread(target=irc_bot.start)
        irc_thread.start()
        return irc_bot

    def create_config():
        if len(sys.argv) != 2:
            fatal_error('Missing JSON configuration file path.')

        cfg_file_name = sys.argv[1]
        logger.info(f'Loading configuration file `{cfg_file_name}`.')

        with open(cfg_file_name) as cfg_file:
            return json.load(cfg_file)

    def configure_signals():
        def sigint_handler(sig, frame):
            logger.info('Got SIGINT.')
            irc_bot.disconnect()
            sys.exit(0)

        signal.signal(signal.SIGINT, sigint_handler)

    _configure_logging()
    logger = logging.getLogger(__name__).getChild('main')
    cfg = create_config()
    repos = create_repos()
    irc_bot = create_irc_bot()
    configure_signals()

    def sleep(duration):
        logger.debug(f'Sleeping {duration} seconds.')
        time.sleep(duration)

    def check_repo_new_commits(repo):
        def msg_commit(commit):
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

        logging.debug(f'Getting new commits for repository {repo.name}.')
        new_commits = repo.get_new_commits()

        if len(new_commits) == 0:
            return

        irc_bot.msg_channel('{} ({})'.format(repo.name, len(new_commits)))
        rate_limit = False

        if len(new_commits) > 5:
            rate_limit = True

        for commit in new_commits:
            msg_commit(commit)

            if rate_limit:
                sleep(1)

    while True:
        for repo in repos:
            check_repo_new_commits(repo)

        sleep(10)
