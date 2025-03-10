#!/usr/bin/env python3
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import os
import sys
from collections import defaultdict
from contextlib import contextmanager, suppress
from typing import (
    TYPE_CHECKING, DefaultDict, Dict, Generator, List, Optional, Sequence,
    Tuple
)

import kitty.fast_data_types as fast_data_types

from .constants import (
    handled_signals, is_freebsd, is_macos, kitty_base_dir, shell_path,
    terminfo_dir
)
from .types import run_once
from .utils import log_error, which

try:
    from typing import TypedDict
except ImportError:
    TypedDict = dict
if TYPE_CHECKING:
    from .window import CwdRequest


if is_macos:
    from kitty.fast_data_types import (
        cmdline_of_process as cmdline_, cwd_of_process as _cwd,
        environ_of_process as _environ_of_process,
        process_group_map as _process_group_map
    )

    def cwd_of_process(pid: int) -> str:
        return os.path.realpath(_cwd(pid))

    def process_group_map() -> DefaultDict[int, List[int]]:
        ans: DefaultDict[int, List[int]] = defaultdict(list)
        for pid, pgid in _process_group_map():
            ans[pgid].append(pid)
        return ans

    def cmdline_of_pid(pid: int) -> List[str]:
        return cmdline_(pid)
else:

    def cmdline_of_pid(pid: int) -> List[str]:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return list(filter(None, f.read().decode('utf-8').split('\0')))

    if is_freebsd:
        def cwd_of_process(pid: int) -> str:
            import subprocess
            cp = subprocess.run(['pwdx', str(pid)], capture_output=True)
            if cp.returncode != 0:
                raise ValueError(f'Failed to find cwd of process with pid: {pid}')
            ans = cp.stdout.decode('utf-8', 'replace').split()[1]
            return os.path.realpath(ans)
    else:
        def cwd_of_process(pid: int) -> str:
            ans = f'/proc/{pid}/cwd'
            return os.path.realpath(ans)

    def _environ_of_process(pid: int) -> str:
        with open(f'/proc/{pid}/environ', 'rb') as f:
            return f.read().decode('utf-8')

    def process_group_map() -> DefaultDict[int, List[int]]:
        ans: DefaultDict[int, List[int]] = defaultdict(list)
        for x in os.listdir('/proc'):
            try:
                pid = int(x)
            except Exception:
                continue
            try:
                with open(f'/proc/{x}/stat', 'rb') as f:
                    raw = f.read().decode('utf-8')
            except OSError:
                continue
            try:
                q = int(raw.split(' ', 5)[4])
            except Exception:
                continue
            ans[q].append(pid)
        return ans


@run_once
def checked_terminfo_dir() -> Optional[str]:
    return terminfo_dir if os.path.isdir(terminfo_dir) else None


def processes_in_group(grp: int) -> List[int]:
    gmap: Optional[DefaultDict[int, List[int]]] = getattr(process_group_map, 'cached_map', None)
    if gmap is None:
        try:
            gmap = process_group_map()
        except Exception:
            gmap = defaultdict(list)
    return gmap.get(grp, [])


@contextmanager
def cached_process_data() -> Generator[None, None, None]:
    try:
        cm = process_group_map()
    except Exception:
        cm = defaultdict(list)
    setattr(process_group_map, 'cached_map', cm)
    try:
        yield
    finally:
        delattr(process_group_map, 'cached_map')


def parse_environ_block(data: str) -> Dict[str, str]:
    """Parse a C environ block of environment variables into a dictionary."""
    # The block is usually raw data from the target process.  It might contain
    # trailing garbage and lines that do not look like assignments.
    ret: Dict[str, str] = {}
    pos = 0

    while True:
        next_pos = data.find("\0", pos)
        # nul byte at the beginning or double nul byte means finish
        if next_pos <= pos:
            break
        # there might not be an equals sign
        equal_pos = data.find("=", pos, next_pos)
        if equal_pos > pos:
            key = data[pos:equal_pos]
            value = data[equal_pos + 1:next_pos]
            ret[key] = value
        pos = next_pos + 1

    return ret


def environ_of_process(pid: int) -> Dict[str, str]:
    return parse_environ_block(_environ_of_process(pid))


def process_env() -> Dict[str, str]:
    ans = dict(os.environ)
    ssl_env_var = getattr(sys, 'kitty_ssl_env_var', None)
    if ssl_env_var is not None:
        ans.pop(ssl_env_var, None)
    ans.pop('KITTY_PREWARM_SOCKET', None)
    return ans


def default_env() -> Dict[str, str]:
    ans: Optional[Dict[str, str]] = getattr(default_env, 'env', None)
    if ans is None:
        return process_env()
    return ans


def set_default_env(val: Optional[Dict[str, str]] = None) -> None:
    env = process_env().copy()
    has_lctype = False
    if val:
        has_lctype = 'LC_CTYPE' in val
        env.update(val)
    setattr(default_env, 'env', env)
    setattr(default_env, 'lc_ctype_set_by_user', has_lctype)


def openpty() -> Tuple[int, int]:
    master, slave = os.openpty()  # Note that master and slave are in blocking mode
    os.set_inheritable(slave, True)
    os.set_inheritable(master, False)
    fast_data_types.set_iutf8_fd(master, True)
    return master, slave


@run_once
def getpid() -> str:
    return str(os.getpid())


class ProcessDesc(TypedDict):
    cwd: Optional[str]
    pid: int
    cmdline: Optional[Sequence[str]]


def is_prewarmable(argv: Sequence[str]) -> bool:
    if len(argv) < 3 or os.path.basename(argv[0]) != 'kitty':
        return False
    if argv[1][:1] not in '@+':
        return False
    if argv[1][0] == '@':
        return True
    if argv[1] == '+':
        return argv[2] != 'open'
    return argv[1] != '+open'


@run_once
def cmdline_of_prewarmer() -> List[str]:
    # we need this check in case the prewarmed process has done an exec and
    # changed its cmdline
    with suppress(Exception):
        return cmdline_of_pid(fast_data_types.get_boss().prewarm.worker_pid)
    return ['']


class Child:

    child_fd: Optional[int] = None
    pid: Optional[int] = None
    forked = False
    is_prewarmed = False

    def __init__(
        self,
        argv: Sequence[str],
        cwd: str,
        stdin: Optional[bytes] = None,
        env: Optional[Dict[str, str]] = None,
        cwd_from: Optional['CwdRequest'] = None,
        allow_remote_control: bool = False,
        is_clone_launch: str = '',
    ):
        self.allow_remote_control = allow_remote_control
        self.is_clone_launch = is_clone_launch
        self.argv = list(argv)
        if cwd_from:
            try:
                cwd = cwd_from.modify_argv_for_launch_with_cwd(self.argv) or cwd
            except Exception as err:
                log_error(f'Failed to read cwd of {cwd_from} with error: {err}')
        else:
            cwd = os.path.expandvars(os.path.expanduser(cwd or os.getcwd()))
        self.cwd = os.path.abspath(cwd)
        self.stdin = stdin
        self.env = env or {}

    def final_env(self) -> Dict[str, str]:
        from kitty.options.utils import DELETE_ENV_VAR
        env = default_env().copy()
        if is_macos and env.get('LC_CTYPE') == 'UTF-8' and not getattr(sys, 'kitty_run_data').get(
                'lc_ctype_before_python') and not getattr(default_env, 'lc_ctype_set_by_user', False):
            del env['LC_CTYPE']
        env.update(self.env)
        env['TERM'] = fast_data_types.get_options().term
        env['COLORTERM'] = 'truecolor'
        env['KITTY_PID'] = getpid()
        if not self.is_prewarmed:
            env['KITTY_PREWARM_SOCKET'] = fast_data_types.get_boss().prewarm.socket_env_var()
            env['KITTY_PREWARM_SOCKET_REAL_TTY'] = ' ' * 32
        if self.cwd:
            # needed in case cwd is a symlink, in which case shells
            # can use it to display the current directory name rather
            # than the resolved path
            env['PWD'] = self.cwd
        tdir = checked_terminfo_dir()
        if tdir:
            env['TERMINFO'] = tdir
        env['KITTY_INSTALLATION_DIR'] = kitty_base_dir
        opts = fast_data_types.get_options()
        self.unmodified_argv = list(self.argv)
        if 'disabled' not in opts.shell_integration:
            from .shell_integration import modify_shell_environ
            modify_shell_environ(opts, env, self.argv)
        env = {k: v for k, v in env.items() if v is not DELETE_ENV_VAR}
        if self.is_clone_launch:
            env['KITTY_IS_CLONE_LAUNCH'] = self.is_clone_launch
            self.is_clone_launch = '1'  # free memory
        else:
            env.pop('KITTY_IS_CLONE_LAUNCH', None)
        return env

    def fork(self) -> Optional[int]:
        if self.forked:
            return None
        self.forked = True
        master, slave = openpty()
        stdin, self.stdin = self.stdin, None
        self.is_prewarmed = is_prewarmable(self.argv)
        if not self.is_prewarmed:
            ready_read_fd, ready_write_fd = os.pipe()
            os.set_inheritable(ready_write_fd, False)
            os.set_inheritable(ready_read_fd, True)
            if stdin is not None:
                stdin_read_fd, stdin_write_fd = os.pipe()
                os.set_inheritable(stdin_write_fd, False)
                os.set_inheritable(stdin_read_fd, True)
            else:
                stdin_read_fd = stdin_write_fd = -1
            env = tuple(f'{k}={v}' for k, v in self.final_env().items())
        argv = list(self.argv)
        exe = argv[0]
        if is_macos and exe == shell_path:
            # bash will only source ~/.bash_profile if it detects it is a login
            # shell (see the invocation section of the bash man page), which it
            # does if argv[0] is prefixed by a hyphen see
            # https://github.com/kovidgoyal/kitty/issues/247
            # it is apparently common to use ~/.bash_profile instead of the
            # more correct ~/.bashrc on macOS to setup env vars, so if
            # the default shell is used prefix argv[0] by '-'
            #
            # it is arguable whether graphical terminals should start shells
            # in login mode in general, there are at least a few Linux users
            # that also make this incorrect assumption, see for example
            # https://github.com/kovidgoyal/kitty/issues/1870
            # xterm, urxvt, konsole and gnome-terminal do not do it in my
            # testing.
            argv[0] = (f'-{exe.split("/")[-1]}')
        self.final_exe = which(exe) or exe
        self.final_argv0 = argv[0]
        if self.is_prewarmed:
            fe = self.final_env()
            self.prewarmed_child = fast_data_types.get_boss().prewarm(slave, self.argv, self.cwd, fe, stdin)
            pid = self.prewarmed_child.child_process_pid
        else:
            pid = fast_data_types.spawn(
                self.final_exe, self.cwd, tuple(argv), env, master, slave, stdin_read_fd, stdin_write_fd,
                ready_read_fd, ready_write_fd, tuple(handled_signals))
        os.close(slave)
        self.pid = pid
        self.child_fd = master
        if not self.is_prewarmed:
            if stdin is not None:
                os.close(stdin_read_fd)
                fast_data_types.thread_write(stdin_write_fd, stdin)
            os.close(ready_read_fd)
            self.terminal_ready_fd = ready_write_fd
        if self.child_fd is not None:
            os.set_blocking(self.child_fd, False)
        return pid

    def __del__(self) -> None:
        fd = getattr(self, 'terminal_ready_fd', -1)
        if fd > -1:
            os.close(fd)
        self.terminal_ready_fd = -1

    def mark_terminal_ready(self) -> None:
        if self.is_prewarmed:
            fast_data_types.get_boss().prewarm.mark_child_as_ready(self.prewarmed_child.child_id)
        else:
            os.close(self.terminal_ready_fd)
            self.terminal_ready_fd = -1

    def cmdline_of_pid(self, pid: int) -> List[str]:
        try:
            ans = cmdline_of_pid(pid)
        except Exception:
            ans = []
        if pid == self.pid and (not ans or (self.is_prewarmed and ans == cmdline_of_prewarmer())):
            ans = list(self.argv)
        return ans

    @property
    def foreground_processes(self) -> List[ProcessDesc]:
        if self.child_fd is None:
            return []
        try:
            pgrp = os.tcgetpgrp(self.child_fd)
            foreground_processes = processes_in_group(pgrp) if pgrp >= 0 else []

            def process_desc(pid: int) -> ProcessDesc:
                ans: ProcessDesc = {'pid': pid, 'cmdline': None, 'cwd': None}
                with suppress(Exception):
                    ans['cmdline'] = self.cmdline_of_pid(pid)
                with suppress(Exception):
                    ans['cwd'] = cwd_of_process(pid) or None
                return ans

            return [process_desc(x) for x in foreground_processes]
        except Exception:
            return []

    @property
    def cmdline(self) -> List[str]:
        try:
            assert self.pid is not None
            return self.cmdline_of_pid(self.pid) or list(self.argv)
        except Exception:
            return list(self.argv)

    @property
    def foreground_cmdline(self) -> List[str]:
        try:
            assert self.pid_for_cwd is not None
            return self.cmdline_of_pid(self.pid_for_cwd) or self.cmdline
        except Exception:
            return self.cmdline

    @property
    def environ(self) -> Dict[str, str]:
        try:
            assert self.pid is not None
            return environ_of_process(self.pid)
        except Exception:
            return {}

    @property
    def current_cwd(self) -> Optional[str]:
        with suppress(Exception):
            assert self.pid is not None
            return cwd_of_process(self.pid)
        return None

    def get_pid_for_cwd(self, oldest: bool = False) -> Optional[int]:
        with suppress(Exception):
            assert self.child_fd is not None
            pgrp = os.tcgetpgrp(self.child_fd)
            foreground_processes = processes_in_group(pgrp) if pgrp >= 0 else []
            if foreground_processes:
                # there is no easy way that I know of to know which process is the
                # foreground process in this group from the users perspective,
                # so we assume the one with the highest PID is as that is most
                # likely to be the newest process. This situation can happen
                # for example with a shell script such as:
                # #!/bin/bash
                # cd /tmp
                # vim
                # With this script , the foreground process group will contain
                # both the bash instance running the script and vim.
                return min(foreground_processes) if oldest else max(foreground_processes)
        return self.pid

    @property
    def pid_for_cwd(self) -> Optional[int]:
        return self.get_pid_for_cwd()

    def get_foreground_cwd(self, oldest: bool = False) -> Optional[str]:
        with suppress(Exception):
            pid = self.get_pid_for_cwd(oldest)
            if pid is not None:
                return cwd_of_process(pid) or None
        return None

    @property
    def foreground_cwd(self) -> Optional[str]:
        return self.get_foreground_cwd()

    @property
    def foreground_environ(self) -> Dict[str, str]:
        pid = self.pid_for_cwd
        if pid is not None:
            with suppress(Exception):
                return environ_of_process(pid)
        pid = self.pid
        if pid is not None:
            with suppress(Exception):
                return environ_of_process(pid)
        return {}

    def send_signal_for_key(self, key_num: int) -> bool:
        import signal
        import termios
        if self.child_fd is None:
            return False
        tty_name = self.foreground_environ.get('KITTY_PREWARM_SOCKET_REAL_TTY')
        if tty_name and tty_name.startswith('/'):
            with open(os.open(tty_name, os.O_RDWR | os.O_CLOEXEC | os.O_NOCTTY, 0)) as real_tty:
                t = termios.tcgetattr(real_tty.fileno())
        else:
            t = termios.tcgetattr(self.child_fd)
        if not t[3] & termios.ISIG:
            return False
        cc = t[-1]
        if key_num == cc[termios.VINTR]:
            s = signal.SIGINT
        elif key_num == cc[termios.VSUSP]:
            s = signal.SIGTSTP
        elif key_num == cc[termios.VQUIT]:
            s = signal.SIGQUIT
        else:
            return False
        pgrp = os.tcgetpgrp(self.child_fd)
        os.killpg(pgrp, s)
        return True
