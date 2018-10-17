# Copyright (c) 2014-2017, iocage
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
"""iocage exec module."""
import subprocess as su

import iocage_lib.ioc_common
import iocage_lib.ioc_json
import iocage_lib.ioc_list
import iocage_lib.ioc_start
import iocage_lib.ioc_exceptions
import select
import fcntl
import os
import re
import collections


class IOCExec(object):
    """Run jexec with a user inside the specified jail."""
    def __init__(self,
                 command,
                 uuid,
                 path,
                 host_user="root",
                 jail_user=None,
                 plugin=False,
                 pkg=False,
                 skip=False,
                 console=False,
                 su_env=None,
                 callback=None):
        self.command = command
        self.uuid = uuid.replace(".", "_") if uuid is not None else uuid
        self.path = path
        self.host_user = host_user
        self.jail_user = jail_user
        self.plugin = plugin
        self.pkg = pkg
        self.skip = skip

        path = '/sbin:/bin:/usr/sbin:/usr/bin:/usr/local/sbin:'\
               '/usr/local/bin:/root/bin'
        su_env = su_env or {}
        su_env.setdefault('PATH', path)
        su_env.setdefault('PWD', '/')
        su_env.setdefault('HOME', '/')

        self.su_env = su_env
        self.callback = callback
        self.cmd = self.command

        if self.uuid is not None:
            self.status, _ = iocage_lib.ioc_list.IOCList().list_get_jid(
                self.uuid)
            self.conf = iocage_lib.ioc_json.IOCJson(self.path).json_load()
            exec_fib = self.conf["exec_fib"]

            self.flight_checks()

            if not self.pkg:
                if self.jail_user:
                    flag = "-U"
                    user = self.jail_user
                else:
                    flag = "-u"
                    user = self.host_user

                self.cmd = [
                    "/usr/sbin/setfib", exec_fib, "jexec", flag, user,
                    f"ioc-{self.uuid}"
                ] + list(self.command)

    def __enter__(self):
        self.proc = su.Popen(
            self.cmd, stdout=su.PIPE, stderr=su.PIPE, close_fds=True,
            bufsize=0, env=self.su_env
        )
        self.exec_gen = self.exec_jail()

        return self.exec_gen

    def __exit__(self, *args):
        try:
            for i in self.exec_gen:
                continue
        except StopIteration:
            pass

        try:
            self.proc.wait(timeout=15)
        except su.TimeoutExpired:
            self.proc.kill()

    def flight_checks(self):
        if not self.status:
            if not self.plugin and not self.skip:
                iocage_lib.ioc_common.logit(
                    {
                        "level": "INFO",
                        "message": f"{self.uuid} is not running,"
                        " starting jail"
                    },
                    _callback=self.callback)

            if self.conf["type"] in (
                    "jail", "plugin", "pluginv2", "clonejail"):
                iocage_lib.ioc_start.IOCStart(
                    self.uuid, self.path, self.conf, silent=True)
            elif self.conf["type"] == "basejail":
                iocage_lib.ioc_common.logit(
                    {
                        "level":
                        "EXCEPTION",
                        "message":
                        "Please run \"iocage migrate\" before trying"
                        f" to start {self.uuid}"
                    },
                    _callback=self.callback)
            elif self.conf["type"] == "template":
                iocage_lib.ioc_common.logit(
                    {
                        "level":
                        "EXCEPTION",
                        "message":
                        "Please convert back to a jail before trying"
                        f" to start {self.uuid}"
                    },
                    _callback=self.callback)
            else:
                iocage_lib.ioc_common.logit(
                    {
                        "level":
                        "EXCEPTION",
                        "message":
                        f"{self.conf['type']} is not a supported jail type."
                    },
                    _callback=self.callback)

            iocage_lib.ioc_common.logit(
                {
                    "level": "INFO",
                    "message": "\nCommand output:"
                },
                _callback=self.callback)

    def exec_jail(self):
        # Courtesy of @william-gr
        # service(8) and some rc.d scripts have the bad habit of
        # exec'ing and never closing stdout/stderr. This makes
        # sure we read only enough until the command exits and do
        # not wait on the pipe to close on the other end.
        #
        # Same issue can be demonstrated with:
        # $ jexec 1 service postgresql onerestart | cat
        # ... <hangs>
        # postgresql rc.d command never closes the pipe
        stderr_queue = collections.deque(maxlen=30)
        rtrn_stdout = _rtrn_stdout = rtrn_stderr = b''

        for i in ('stdout', 'stderr'):
            fileno = getattr(self.proc, i).fileno()
            fl = fcntl.fcntl(fileno, fcntl.F_GETFL)
            fcntl.fcntl(fileno, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        timeout = 0.1

        while True:
            r = select.select([
                self.proc.stdout.fileno(),
                self.proc.stderr.fileno()], [], [], timeout)[0]

            if self.proc.poll() is not None:
                if timeout == 0:
                    break
                else:
                    timeout = 0

            if r:
                if self.proc.stdout.fileno() in r:
                    rtrn_stdout = self.proc.stdout.read()

                    if rtrn_stdout:
                        _rtrn_stdout = rtrn_stdout
                if self.proc.stderr.fileno() in r:
                    rtrn_stderr = self.proc.stderr.read()
                    stderr_queue.append(rtrn_stderr)

                yield rtrn_stdout, rtrn_stderr

        error = True if self.proc.returncode != 0 else False

        # self.uuid being None means a release being updated,
        # We will get false positives for EOL notices
        if error and self.uuid is not None:
            # EOL notice for jail updates
            jail_eol_regex = \
                rb'(WARNING: FreeBSD \d*\.\d-RELEASE HAS PASSED ITS'\
                rb' END-OF-LIFE DATE)'

            if re.search(jail_eol_regex, _rtrn_stdout):
                error = False

            if error:
                raise iocage_lib.ioc_exceptions.CommandFailed(
                    list(stderr_queue))


class SilentExec(object):
    def __init__(self, *args, **kwargs):
        with IOCExec(*args, **kwargs) as silent:  # noqa
            pass
