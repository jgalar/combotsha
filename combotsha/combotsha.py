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

import sys
import signal
import threading
import time
import yaml
import tempfile
import glob
import git  # type: ignore
import git.exc  # type: ignore
import irc  # type: ignore
import irc.bot  # type: ignore
import logging
import os
from typing import Optional, Iterator, List, Mapping, Any, Union


def _format_commit(
    commit: git.Commit,
    before_dt: str = '',
    after_dt: str = '',
    before_hash: str = '',
    after_hash: str = '',
    before_summary: str = '',
    after_summary: str = '',
    before_author: str = '',
    after_author: str = '',
    before_insertions: str = '',
    after_insertions: str = '',
    before_deletions: str = '',
    after_deletions: str = '',
) -> str:
    dt_str = commit.authored_datetime.strftime('%Y-%m-%d %H:%M')
    dt = f'{before_dt}{dt_str}{after_dt}'
    hash = f'{before_hash}{commit.hexsha[:8]}{after_hash}'
    summary = f'{before_summary}{commit.summary}{after_summary}'
    author = f'{before_author}{commit.author.name}{after_author}'
    insertions = (
        f'{before_insertions}+{commit.stats.total["insertions"]}{after_insertions}'
    )
    deletions = f'{before_deletions}-{commit.stats.total["deletions"]}{after_deletions}'
    return f'{dt}: [{author}] {hash} {summary} ({insertions} {deletions})'


class _Repository:
    def __init__(self, name: str, url: str, last_seen_commit_sha: Optional[str] = None):
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
    def _commit_iter(self) -> Iterator[git.Commit]:
        return self._repo.iter_commits('origin/master')

    @property
    def name(self) -> str:
        return self._name

    def get_new_commits(self) -> List[git.Commit]:
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

        return list(reversed(new_commits))


class _IrcBot(irc.bot.SingleServerIRCBot):
    def __init__(self, channel_name: str, nick: str, server: str, port: int = 6667):
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

    def msg_channel(self, msg: str):
        if self._connection is None:
            # not connected yet
            return

        self._logger.info(f'Sending private message to channel `{self._channel_name}`.')
        self._connection.privmsg(self._channel_name, msg)

    def disconnect_from_server(self):
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
    def fatal_error(msg: str):
        logger.setLevel(logging.CRITICAL)
        logger.critical(msg)
        sys.exit(1)

    def create_repos() -> List[_Repository]:
        cfg_repos = cfg['repos']
        repos = []

        for repo_cfg in cfg_repos:
            repos.append(
                _Repository(
                    repo_cfg['name'], repo_cfg['url'], repo_cfg.get('last-commit-sha'),
                )
            )

        return repos

    def create_irc_bot() -> _IrcBot:
        cfg_irc = cfg['irc']
        irc_bot = _IrcBot(
            cfg_irc['channel'],
            cfg_irc.get('nick', 'combotsha'),
            cfg_irc['server'],
            cfg_irc.get('port', 6667),
        )
        logger.info('Starting IRC bot thread.')
        irc_thread = threading.Thread(target=irc_bot.start)
        irc_thread.start()
        return irc_bot

    def create_config() -> Mapping[str, Any]:
        if len(sys.argv) != 2:
            fatal_error('Missing YAML configuration file path.')

        cfg_file_name = sys.argv[1]
        logger.info(f'Loading configuration file `{cfg_file_name}`.')

        with open(cfg_file_name) as cfg_file:
            return yaml.load(cfg_file, Loader=yaml.Loader)

    def configure_signals():
        def sigint_handler(sig, frame):
            logger.info('Got SIGINT.')
            irc_bot.disconnect_from_server()
            sys.exit(0)

        signal.signal(signal.SIGINT, sigint_handler)

    _configure_logging()
    logger = logging.getLogger(__name__).getChild('main')
    cfg = create_config()
    repos = create_repos()
    irc_bot = create_irc_bot()
    configure_signals()

    def sleep(duration: Union[int, float]):
        logger.debug(f'Sleeping {duration} seconds.')
        time.sleep(duration)

    def check_repo_new_commits(repo: _Repository):
        def msg_commit(commit: git.Commit):
            commit_str = _format_commit(
                commit,
                before_dt='\x02\x0312',
                after_dt='\x0f',
                before_hash='\x0307',
                after_hash='\x0f',
                before_summary='\x0f',
                after_summary='\x0f',
                before_author='\x0303',
                after_author='\x0f',
                before_insertions='\x02\x0309',
                after_insertions='\x0f',
                before_deletions='\x02\x0304',
                after_deletions='\x0f',
            )

            irc_bot.msg_channel(f'\x02{repo.name}\x0f: {commit_str}')

        logging.debug(f'Getting new commits for repository {repo.name}.')
        new_commits = repo.get_new_commits()

        if len(new_commits) == 0:
            return

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
