"""tmux session manager for Claude Code bridge."""
import subprocess
import os
import pty
import fcntl
import struct
import termios
import time

import config


def session_name_for_project(project_id: str) -> str:
    """Return tmux session name for a given project ID."""
    return f"claude-bridge-{project_id}"


def _run(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.stdout.strip()


def session_exists(session: str) -> bool:
    return _run(f"tmux has-session -t {session} 2>&1 && echo yes || echo no") == "yes"


def _build_backend_cmd(binary: str, args: str = "") -> str:
    """Build tmux command with optional proxy."""
    proxy = config.PROXY or ""
    proxy_part = f"HTTPS_PROXY={proxy} HTTP_PROXY={proxy} " if proxy else ""
    return f"unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT; {proxy_part}{binary} {args}; stty sane; tput reset".strip()


def get_backend_cmds() -> dict:
    skip_flag = "--dangerously-skip-permissions" if config.CLAUDE_SKIP_PERMISSIONS else ""
    cmds = {
        "claude": _build_backend_cmd(config.CLAUDE_BIN, skip_flag),
    }
    if config.RALPH_AVAILABLE:
        cmds["ralph"] = _build_backend_cmd("ralph", "plan")
    return cmds


def _clean_tmux_env(session: str):
    """Remove Claude Code env vars from tmux session so new windows don't inherit them."""
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        subprocess.run(f"tmux set-environment -t {session} -u {var} 2>/dev/null", shell=True)
        subprocess.run(f"tmux set-environment -g -u {var} 2>/dev/null", shell=True)


def ensure_session(session: str, workdir: str = None):
    """Create tmux session with claude CLI if not exists."""
    workdir = workdir or os.path.expanduser("~")
    if session_exists(session):
        _clean_tmux_env(session)
        return
    os.makedirs(workdir, exist_ok=True)
    subprocess.run(
        f"tmux new-session -d -s {session} -c {workdir} -x {config.TMUX_COLS} -y {config.TMUX_ROWS}",
        shell=True,
    )
    # Enable aggressive-resize so each window sizes to its own active client
    subprocess.run(
        f"tmux set-window-option -g aggressive-resize on",
        shell=True,
    )
    _clean_tmux_env(session)
    _run(f"tmux rename-window -t {session}:0 'claude'")
    time.sleep(0.5)
    claude_cmd = get_backend_cmds()["claude"]
    subprocess.run(
        f"tmux send-keys -t {session} '{claude_cmd}' Enter",
        shell=True,
    )


def list_windows(session: str):
    """List all tmux windows in the session."""
    result = _run(f"tmux list-windows -t {session} -F '#{{window_index}}:#{{window_name}}'")
    if not result:
        return []
    windows = []
    for line in result.strip().split("\n"):
        idx, name = line.split(":", 1)
        windows.append({"index": int(idx), "name": name})
    return windows


def window_exists(session: str, window_idx: int) -> bool:
    """Check if a specific tmux window exists."""
    result = _run(f"tmux list-windows -t {session} -F '#{{window_index}}'")
    if not result:
        return False
    existing = {int(x) for x in result.strip().split("\n")}
    return window_idx in existing


def create_window(session: str, workdir: str = None, backend: str = "claude"):
    """Create a new tmux window with the specified backend. Returns window index."""
    workdir = workdir or os.path.expanduser("~")
    ensure_session(session, workdir)
    _clean_tmux_env(session)
    result = _run(f"tmux new-window -t {session} -c {workdir} -P -F '#{{window_index}}'")
    window_idx = int(result.strip())
    _run(f"tmux rename-window -t {session}:{window_idx} '{backend}'")
    cmds = get_backend_cmds()
    cmd = cmds.get(backend, cmds["claude"])
    time.sleep(0.5)
    subprocess.run(
        f"tmux send-keys -t {session}:{window_idx} '{cmd}' Enter",
        shell=True,
    )
    return window_idx


def close_window(session: str, window_idx: int):
    """Close a tmux window."""
    subprocess.run(f"tmux kill-window -t {session}:{window_idx}", shell=True)


_link_counter = 0

def get_pty_fd_for_window(session: str, window_idx: int = 0, cols: int = None, rows: int = None):
    cols = cols or config.TMUX_COLS
    rows = rows or config.TMUX_ROWS
    """Create a PTY connected to a specific tmux window via a dedicated sub-session."""
    global _link_counter
    _link_counter += 1
    # Create a linked session that targets a specific window
    # Each connection gets a unique linked session to avoid conflicts
    link_name = f"{session}-link-{window_idx}-{os.getpid()}-{_link_counter}"
    subprocess.run(
        f"tmux new-session -d -s {link_name} -t {session} 2>/dev/null",
        shell=True,
    )
    # Select the correct window in the linked session
    subprocess.run(
        f"tmux select-window -t {link_name}:{window_idx} 2>/dev/null",
        shell=True,
    )

    master_fd, slave_fd = pty.openpty()
    # Set PTY size BEFORE tmux attach so it registers at the right size
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    proc = subprocess.Popen(
        ["tmux", "attach-session", "-t", link_name],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=env,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return master_fd, proc, link_name


def send_interrupt(session: str):
    """Send Ctrl+C to interrupt Claude Code."""
    subprocess.run(f"tmux send-keys -t {session} C-c", shell=True)


def kill_session(session: str):
    """Kill the tmux session."""
    subprocess.run(f"tmux kill-session -t {session}", shell=True)


def resize_pty(fd: int, cols: int, rows: int, link_name: str = None):
    """Resize PTY and notify tmux to re-read client size."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    if link_name:
        subprocess.run(
            f"tmux refresh-client -t {link_name} 2>/dev/null",
            shell=True, capture_output=True,
        )


