import os
import re
import shutil
import subprocess
import threading
import uuid

import sublime
import sublime_plugin


# In-memory cache only. Authoritative state lives on view.settings() so it
# survives plugin reloads (saving claude_proxy.py wipes module-level globals).
SESSIONS = {}

CLAUDE_FALLBACK_PATHS = [
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.claude/local/claude",
    "~/.local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.volta/bin/claude",
]

PROMPT_MARKER = "> "
DIVIDER = "\n\n"

IN_BUFFER_COMMANDS = {"<CONTINUE>", "<PLAN>", "<ACCEPT>", "<DEFAULT>"}

UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
SESSION_FILENAME_RE = re.compile(r"ClaudeSession-[0-9a-f-]{36}\.txt$")

BANNER_TEMPLATE = (
    "Claude session {sid}\n"
    "cwd: {cwd}\n"
    "\n"
    "Commands (type one alone after >, then Ctrl+Enter):\n"
    "  <CONTINUE>  Re-attach to this session after reopening the file.\n"
    "  <PLAN>      Switch to plan mode: claude analyzes; no edits or tool runs.\n"
    "  <ACCEPT>    Switch to accept-edits mode: auto-approve file edits.\n"
    "  <DEFAULT>   Restore the default permission prompts.\n"
    "\n"
    "Type your prompt after the > below and press Ctrl+Enter.\n"
    "\n"
    "> "
)


def format_banner(session_id, cwd):
    return BANNER_TEMPLATE.format(sid=session_id, cwd=cwd)


def resolve_claude_executable(name):
    if os.path.isabs(name):
        return name
    found = shutil.which(name)
    if found:
        return found
    for candidate in CLAUDE_FALLBACK_PATHS:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return name


def resolve_claude_cmd():
    settings = sublime.load_settings("ClaudeProxy.sublime-settings")
    cmd = settings.get("claude_command", ["claude"])
    if isinstance(cmd, str):
        cmd = [cmd]
    cmd = list(cmd)
    cmd[0] = resolve_claude_executable(cmd[0])
    return cmd


def pick_cwd(window):
    for folder in window.folders():
        return folder
    active = window.active_view()
    if active and active.file_name():
        return os.path.dirname(active.file_name())
    return os.path.expanduser("~")


def extract_pending_prompt(view):
    """Return text typed after the last '\\n> ' marker (or buffer start)."""
    text = view.substr(sublime.Region(0, view.size()))
    needle = "\n" + PROMPT_MARKER
    idx = text.rfind(needle)
    if idx < 0:
        if text.startswith(PROMPT_MARKER):
            return text[len(PROMPT_MARKER):]
        return ""
    return text[idx + len(needle):]


def find_session_id_in_view(view):
    head = view.substr(sublime.Region(0, min(view.size(), 1024)))
    m = UUID_RE.search(head)
    if m:
        return m.group(1)
    fname = view.file_name() or ""
    m = UUID_RE.search(fname)
    if m:
        return m.group(1)
    return None


def append_to_view(view, text):
    if not view.is_valid():
        return
    view.run_command("claude_proxy_append", {"text": text})


def store_session_state(view, session_id, cmd, cwd, first_turn):
    s = view.settings()
    s.set("claude_proxy_active", True)
    s.set("claude_session_id", session_id)
    s.set("claude_session_cmd", cmd)
    s.set("claude_session_cwd", cwd)
    s.set("claude_session_first_turn", first_turn)


class ClaudeSession:
    def __init__(self, view, cmd, cwd, session_id):
        self.view = view
        self.cmd = cmd
        self.cwd = cwd
        self.session_id = session_id
        self.is_first_turn = True
        self.current_process = None
        self.busy = False

    def send(self, prompt):
        if self.busy:
            sublime.status_message(
                "ClaudeProxy: still waiting for previous response"
            )
            return
        if not prompt.strip():
            return
        self.busy = True
        args = list(self.cmd) + ["--print"]
        pm = self.view.settings().get("claude_permission_mode")
        if pm:
            args += ["--permission-mode", pm]
        if self.is_first_turn:
            args += ["--session-id", self.session_id]
        else:
            args += ["--resume", self.session_id]
        args += [prompt]
        self.is_first_turn = False
        self.view.settings().set("claude_session_first_turn", False)
        threading.Thread(target=self._run, args=(args,), daemon=True).start()

    def _run(self, args):
        sublime.set_timeout(lambda: append_to_view(self.view, DIVIDER), 0)
        env = os.environ.copy()
        env.setdefault("TERM", "dumb")
        env.setdefault("NO_COLOR", "1")
        print("[ClaudeProxy] cwd={} args={}".format(self.cwd, args))
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.cwd,
                env=env,
                bufsize=0,
            )
        except FileNotFoundError as e:
            sublime.set_timeout(
                lambda: self._finalize_turn("\n[launch failed: {}]".format(e)),
                0,
            )
            return
        print("[ClaudeProxy] launched pid={}".format(proc.pid))
        self.current_process = proc
        try:
            while True:
                chunk = proc.stdout.read(512)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                sublime.set_timeout(
                    lambda t=text: append_to_view(self.view, t), 0
                )
            proc.wait()
            if proc.returncode != 0:
                sublime.set_timeout(
                    lambda rc=proc.returncode: append_to_view(
                        self.view,
                        "\n[claude exited with code {}]".format(rc),
                    ),
                    0,
                )
        finally:
            self.current_process = None
            sublime.set_timeout(self._finalize_turn, 0)

    def _finalize_turn(self, extra=""):
        if extra:
            append_to_view(self.view, extra)
        append_to_view(self.view, DIVIDER + PROMPT_MARKER)
        self.busy = False

    def cancel(self):
        if self.current_process and self.current_process.poll() is None:
            try:
                self.current_process.terminate()
            except OSError:
                pass

    def stop(self):
        self.cancel()


def get_or_restore_session(view):
    session = SESSIONS.get(view.id())
    if session:
        return session
    s = view.settings()
    sid = s.get("claude_session_id")
    cmd = s.get("claude_session_cmd")
    cwd = s.get("claude_session_cwd")
    if not (sid and cmd and cwd):
        return None
    session = ClaudeSession(view, cmd, cwd, sid)
    session.is_first_turn = bool(s.get("claude_session_first_turn", False))
    SESSIONS[view.id()] = session
    return session


def handle_in_buffer_command(view, token):
    s = view.settings()
    if token == "<CONTINUE>":
        sid = find_session_id_in_view(view)
        if not sid:
            append_to_view(
                view,
                DIVIDER
                + "[<CONTINUE> failed: no session ID found in banner or filename]"
                + DIVIDER
                + PROMPT_MARKER,
            )
            return
        cmd = resolve_claude_cmd()
        if not (os.path.isabs(cmd[0]) and os.path.isfile(cmd[0])):
            append_to_view(
                view,
                DIVIDER
                + "[<CONTINUE> failed: claude executable not found: {}]".format(cmd[0])
                + DIVIDER
                + PROMPT_MARKER,
            )
            return
        cwd = s.get("claude_session_cwd")
        if not cwd:
            fname = view.file_name()
            cwd = os.path.dirname(fname) if fname else os.path.expanduser("~")
        store_session_state(view, sid, cmd, cwd, first_turn=False)
        SESSIONS.pop(view.id(), None)
        append_to_view(
            view,
            DIVIDER
            + "[attached to session {}]".format(sid)
            + DIVIDER
            + PROMPT_MARKER,
        )
        return
    if token == "<PLAN>":
        s.set("claude_permission_mode", "plan")
        append_to_view(
            view,
            DIVIDER + "[permission mode: plan]" + DIVIDER + PROMPT_MARKER,
        )
        return
    if token == "<ACCEPT>":
        s.set("claude_permission_mode", "acceptEdits")
        append_to_view(
            view,
            DIVIDER + "[permission mode: acceptEdits]" + DIVIDER + PROMPT_MARKER,
        )
        return
    if token == "<DEFAULT>":
        s.erase("claude_permission_mode")
        append_to_view(
            view,
            DIVIDER + "[permission mode: default]" + DIVIDER + PROMPT_MARKER,
        )
        return


class ClaudeProxyAppendCommand(sublime_plugin.TextCommand):
    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), text)
        self.view.show(self.view.size())


class ClaudeProxyStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        cmd = resolve_claude_cmd()
        if not (os.path.isabs(cmd[0]) and os.path.isfile(cmd[0])):
            sublime.error_message(
                "ClaudeProxy: could not find `{}`.\n"
                "Install the Claude Code CLI or set `claude_command` in\n"
                "Preferences > Package Settings > ClaudeProxy.".format(cmd[0])
            )
            return

        cwd = pick_cwd(self.window)
        session_id = str(uuid.uuid4())
        file_path = os.path.join(
            cwd, "ClaudeSession-{}.txt".format(session_id)
        )
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(format_banner(session_id, cwd))
        except OSError as e:
            sublime.error_message(
                "ClaudeProxy: could not create {}:\n{}".format(file_path, e)
            )
            return

        view = self.window.open_file(file_path)
        view.settings().set("word_wrap", True)
        store_session_state(view, session_id, cmd, cwd, first_turn=True)
        SESSIONS[view.id()] = ClaudeSession(view, cmd, cwd, session_id)


class ClaudeProxySendCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        raw = extract_pending_prompt(view)
        prompt = raw.strip()
        if not prompt:
            return
        upper = prompt.upper()
        if upper in IN_BUFFER_COMMANDS:
            handle_in_buffer_command(view, upper)
            return
        session = get_or_restore_session(view)
        if not session:
            sublime.status_message(
                "ClaudeProxy: no active session. Type <CONTINUE> to resume "
                "from this file, or run 'ClaudeProxy: Start Session'."
            )
            return
        session.send(prompt)


class ClaudeProxyCancelCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            return
        session = get_or_restore_session(view)
        if session:
            session.cancel()


class ClaudeProxyStopCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            return
        session = SESSIONS.pop(view.id(), None)
        if session:
            session.stop()


class ClaudeProxyListener(sublime_plugin.EventListener):
    def on_load(self, view):
        self._maybe_activate(view)

    def on_activated(self, view):
        self._maybe_activate(view)

    def _maybe_activate(self, view):
        s = view.settings()
        if s.get("claude_proxy_active"):
            return
        fname = view.file_name() or ""
        if SESSION_FILENAME_RE.search(fname):
            s.set("claude_proxy_active", True)

    def on_close(self, view):
        session = SESSIONS.pop(view.id(), None)
        if session:
            session.stop()
