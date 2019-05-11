#!/usr/bin/env python3
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

class Repository:
    def __init__(self, name, url, last_seen_commit_sha=None):
        self._name = name
        self._url = url
        self._directory = tempfile.TemporaryDirectory()
        self._repo = None
        self._last_seen_commit_sha = last_seen_commit_sha

        print('Cloning ' + self._name)
        git.Git(self._directory.name).clone(self._url)
        self._repo = git.Repo(glob.glob(self._directory.name + '/*/')[0])

    @property
    def name(self):
        return self._name

    def get_new_commits(self):
        if self._last_seen_commit_sha is None:
            for commit in self._repo.iter_commits('origin/master'):
                self._last_seen_commit_sha = commit.hexsha
                return []

        self._repo.remotes.origin.fetch()
        new_commits = []
        for commit in self._repo.iter_commits('origin/master'):
            if commit.hexsha.startswith(self._last_seen_commit_sha):
                break
            new_commits.append(commit)
        if len(new_commits) > 0:
            self._last_seen_commit_sha = new_commits[0].hexsha
        return new_commits


class IRCSession:
    class NicknameInUse(Exception):
        pass

    def __init__(self, server_url, server_port, channel_name, nickname):
        print('Connecting to {}...'.format(server_url))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((server_url, server_port))
        self._sock = sock
        self._channel = channel_name
        self._status_pattern = re.compile('^.* ([0-9][0-9][0-9]) .*')
        self._reception_buffer = bytearray()
        self._nickname = nickname

    def _send_command(self, cmd_name, cmd_payload):
        payload = '{} {}\r\n'.format(cmd_name, cmd_payload).encode('utf-8')
        self._sock.send(payload)

    def _identify(self, nickname):
        print('Identifying as ' + nickname)
        self._send_command('NICK', nickname)
        self._send_command('USER', '{} * * :{}'.format(nickname, nickname))

    def _join_channel(self, channel):
        print('Joining ' + channel)
        self._send_command('JOIN', channel)
        self._channel = channel

    def message_channel(self, msg):
        self._send_command('PRIVMSG ' + self._channel, ':' + msg)

    def _pop_message(self):
        msg = None
        if len(self._reception_buffer) > 1:
            for i in range(1, len(self._reception_buffer)):
                # Look for a message boundary (\r\n)
                if self._reception_buffer[i - 1:i + 1] == b'\r\n':
                    msg = self._reception_buffer[:i - 1].decode('utf-8')
                    del self._reception_buffer[:i + 1]
                    break
        return msg

    def _receive(self):
        while True:
            msg = self._pop_message()
            if msg is not None:
                return msg
            # Max message length, including CRLF, according to RFC 2812
            self._reception_buffer += self._sock.recv(512)

    def _pong(self, payload):
        reply = payload.split(":")[1]
        self._send_command('PONG', ':' + reply)

    def quit(self):
        self._send_command('QUIT', ':Bye-bye-bye-bye-bye-bye-bye!!')

    def _get_payload_status(self, payload):
        status = None
        try:
            m = self._status_pattern.match(payload)
            status = int(m[1])
        except:
            pass
        return status

    def sign_in(self):
        # Try to join server and channel
        while True:
            payload = self._receive()
            status = self._get_payload_status(payload)
            if "No Ident response" in payload:
                self._identify(self._nickname)
            elif status == 433:
                print('Nickname already in use... BYYYYYYE!')
                session.quit()
                raise self.NicknameInUse
            elif status == 376:
                self._join_channel(self._channel)
            elif status == 366:
                break

    def _handle_message(self, payload):
        if 'PING' in payload:
            self._pong(payload)

    def run(self):
        while True:
            self._handle_message(self._receive())


if len(sys.argv) != 2:
    print('Usage: combotcha config.json')
    sys.exit(1)

cfg = None
with open(sys.argv[1]) as cfg_file:
    cfg = json.load(cfg_file)

irc_cfg = cfg['irc']
session = IRCSession(irc_cfg['url'], irc_cfg['port'], irc_cfg['channel'],
                     irc_cfg['nick'])
session.sign_in()

repos = []
repos_cfg = cfg['repos']
for repo_cfg in repos_cfg:
    repo = Repository(repo_cfg['name'], repo_cfg['url'],
                      repo_cfg.get('last_seen_commit_sha', None))
    repos.append(repo)


def sigint_handler(sig, frame):
    session.quit()
    sys.exit(0)

signal.signal(signal.SIGINT, sigint_handler)

print('Launching IRC thread')
irc_thread = threading.Thread(target=session.run)
irc_thread.start()

while True:
    for repo in repos:
        new_commits = repo.get_new_commits()
        if not new_commits:
            continue

        print('{} new commits found for {} repository'.format(
            len(new_commits), repo.name))
        session.message_channel('{} ({})'.format(repo.name, len(new_commits)))
        for commit in new_commits:
            session.message_channel('\x0307{} \x0300{} \x0303[{}]'.format(
                commit.hexsha[:8], commit.summary, commit.author.name))
    time.sleep(10)
