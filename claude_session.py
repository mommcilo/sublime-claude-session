import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid

import sublime
import sublime_plugin


# In-memory cache only. Authoritative state lives on view.settings() so it
# survives plugin reloads (saving claude_session.py wipes module-level globals).
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

DIR_PROMPT_LABEL = (
    "-> Enter working directory(current - if you want to work from here "
    "just hit CTRL + ENTER): "
)
DIR_CHECK_PROMPT = (
    "Check if folder {path} exists. If not don't do anything just inform "
    "user. If it exist move yourself to that folder we will work from there."
)

IN_BUFFER_COMMANDS = {"<CONTINUE>", "<PLAN>", "<ACCEPT>", "<DEFAULT>"}

INDICATOR_TICK_MS = 120
INDICATOR_FRAMES = [
    "[●○○○○]",
    "[○●○○○]",
    "[○○●○○]",
    "[○○○●○]",
    "[○○○○●]",
    "[○○○●○]",
    "[○○●○○]",
    "[○●○○○]",
]
INDICATOR_HTML = (
    "<body id=\"claude-session-indicator\">"
    "<div style=\"padding: 2px 6px;"
    " color: color(var(--foreground) alpha(0.7));\">"
    "<div style=\"font-family: monospace;\">elapsed {elapsed}</div>"
    "<div style=\"font-family: monospace; font-size: 0.9em;\">{spinner}</div>"
    "{activity}"
    "</div></body>"
)
ACTIVITY_LINE_HTML = (
    "<div style=\"font-family: monospace; font-size: 0.85em;"
    " opacity: 0.85; padding-top: 1px;\">{text}</div>"
)
ACTIVITY_MAX_LEN = 80

UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
SESSION_FILENAME_RE = re.compile(r"ClaudeSession-[0-9a-f-]{36}\.txt$")

BANNER_TEMPLATE = (
    "Claude session {sid}\n"
    "-> cwd: {cwd}\n"
    "\n"
    "Commands (type one alone after >, then Ctrl+Enter):\n"
    "  <CONTINUE>  Re-attach to this session after reopening the file.\n"
    "  <PLAN>      Switch to plan mode: claude analyzes; no edits or tool runs.\n"
    "  <ACCEPT>    Switch to accept-edits mode: auto-approve file edits.\n"
    "  <DEFAULT>   Restore the default permission prompts.\n"
    "\n"
    "{dir_prompt_label}{cwd}"
)


def format_banner(session_id, cwd):
    return BANNER_TEMPLATE.format(
        sid=session_id, cwd=cwd, dir_prompt_label=DIR_PROMPT_LABEL
    )


def extract_directory_prompt(view):
    text = view.substr(sublime.Region(0, view.size()))
    idx = text.rfind(DIR_PROMPT_LABEL)
    if idx < 0:
        return None
    return text[idx + len(DIR_PROMPT_LABEL):]


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
    settings = sublime.load_settings("ClaudeSession.sublime-settings")
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
    view.run_command("claude_session_append", {"text": text})


def store_session_state(view, session_id, cmd, cwd, first_turn):
    s = view.settings()
    s.set("claude_session_active", True)
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
        self.start_time = None
        self.phantom_set = None
        self.spinner_frame = 0
        self.current_activity = ""
        self.response_timestamp_written = False

    def send(self, prompt):
        if self.busy:
            sublime.status_message(
                "ClaudeSession: still waiting for previous response"
            )
            return
        if not prompt.strip():
            return
        self.view.run_command(
            "claude_session_insert_above_last_prompt",
            {"text": "[" + self._timestamp() + "]\n"},
        )
        self.busy = True
        self.start_time = time.monotonic()
        self.spinner_frame = 0
        self.current_activity = ""
        self.response_timestamp_written = False
        sublime.set_timeout(self._indicator_tick, 0)
        args = list(self.cmd) + [
            "--print",
            "--output-format", "stream-json",
            "--verbose",
        ]
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
        print("[ClaudeSession] cwd={} args={}".format(self.cwd, args))
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.cwd,
                env=env,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
            )
        except FileNotFoundError as e:
            sublime.set_timeout(
                lambda: self._finalize_turn("\n[launch failed: {}]".format(e)),
                0,
            )
            return
        print("[ClaudeSession] launched pid={}".format(proc.pid))
        self.current_process = proc
        try:
            for line in proc.stdout:
                stripped = line.rstrip("\r\n")
                if not stripped:
                    continue
                event = None
                if stripped.startswith("{"):
                    try:
                        event = json.loads(stripped)
                    except ValueError:
                        event = None
                if event is None:
                    self._emit_buffer_text(stripped + "\n")
                    continue
                self._handle_event(event)
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

    def _emit_buffer_text(self, text):
        sublime.set_timeout(
            lambda t=text: append_to_view(self.view, t), 0
        )

    def _emit_response_timestamp(self):
        if self.response_timestamp_written:
            return
        self.response_timestamp_written = True
        ts_line = "\n[" + self._timestamp() + "]\n"
        sublime.set_timeout(
            lambda t=ts_line: append_to_view(self.view, t), 0
        )

    def _timestamp(self):
        return time.strftime("%Y-%m-%d %H:%M:%S")

    def _handle_event(self, event):
        etype = event.get("type")
        if etype == "assistant":
            message = event.get("message") or {}
            content = message.get("content") or []
            parent = event.get("parent_tool_use_id")
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text") or ""
                    if text:
                        self._emit_buffer_text(text)
                elif btype == "tool_use":
                    name = block.get("name") or ""
                    tool_input = block.get("input") or {}
                    self.current_activity = self._describe_tool_use(
                        name, tool_input, parent
                    )
            return
        if etype == "user":
            return
        if etype == "result":
            self._emit_response_timestamp()
            return
        return

    def _describe_tool_use(self, name, tool_input, parent_tool_use_id):
        if not isinstance(tool_input, dict):
            tool_input = {}
        prefix = "↳ " if parent_tool_use_id else ""
        if name == "Task":
            subagent = tool_input.get("subagent_type") or "agent"
            desc = tool_input.get("description") or ""
            body = "→ {} subagent: {}".format(subagent, desc)
        elif name == "Bash":
            cmd = tool_input.get("command") or ""
            body = "Bash: {}".format(cmd)
        elif name in ("Read", "Edit", "Write", "NotebookEdit"):
            path = tool_input.get("file_path") or ""
            body = "{}: {}".format(name, os.path.basename(path) or path)
        elif name in ("Glob", "Grep"):
            pat = tool_input.get("pattern") or ""
            body = "{}: {}".format(name, pat)
        elif name == "WebFetch":
            url = tool_input.get("url") or ""
            body = "WebFetch: {}".format(url)
        elif name == "WebSearch":
            q = tool_input.get("query") or ""
            body = "WebSearch: {}".format(q)
        else:
            body = name or "tool"
        full = prefix + body
        full = full.replace("\n", " ")
        if len(full) > ACTIVITY_MAX_LEN:
            full = full[: ACTIVITY_MAX_LEN - 1] + "…"
        return full

    def _finalize_turn(self, extra=""):
        self._clear_indicator()
        if not self.response_timestamp_written:
            self.response_timestamp_written = True
            append_to_view(
                self.view, "\n[" + self._timestamp() + "]\n"
            )
        if extra:
            append_to_view(self.view, extra)
        append_to_view(self.view, DIVIDER + PROMPT_MARKER)
        self.busy = False

    def _indicator_tick(self):
        if not self.busy or not self.view.is_valid():
            self._clear_indicator()
            return
        if SESSIONS.get(self.view.id()) is not self:
            self._clear_indicator()
            return
        if self.phantom_set is None:
            self.phantom_set = sublime.PhantomSet(
                self.view, "claude_session_indicator"
            )
        elapsed = int(time.monotonic() - (self.start_time or time.monotonic()))
        mm = elapsed // 60
        ss = elapsed % 60
        elapsed_text = "{:02d}:{:02d}".format(mm, ss)
        spinner = INDICATOR_FRAMES[self.spinner_frame % len(INDICATOR_FRAMES)]
        self.spinner_frame += 1
        if self.current_activity:
            activity_html = ACTIVITY_LINE_HTML.format(
                text=html.escape(self.current_activity)
            )
        else:
            activity_html = ""
        body = INDICATOR_HTML.format(
            elapsed=elapsed_text,
            spinner=spinner,
            activity=activity_html,
        )
        region = sublime.Region(self.view.size(), self.view.size())
        phantom = sublime.Phantom(region, body, sublime.LAYOUT_BLOCK)
        self.phantom_set.update([phantom])
        sublime.set_timeout(self._indicator_tick, INDICATOR_TICK_MS)

    def _clear_indicator(self):
        if self.phantom_set is not None and self.view.is_valid():
            try:
                self.phantom_set.update([])
            except Exception as e:
                print("[ClaudeSession] indicator clear failed: {}".format(e))
        self.phantom_set = None
        self.start_time = None
        self.current_activity = ""

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


class ClaudeSessionAppendCommand(sublime_plugin.TextCommand):
    def run(self, edit, text):
        self.view.insert(edit, self.view.size(), text)
        self.view.show(self.view.size())


class ClaudeSessionInsertAboveLastPromptCommand(sublime_plugin.TextCommand):
    def run(self, edit, text):
        view = self.view
        if not view.is_valid():
            return
        full_text = view.substr(sublime.Region(0, view.size()))
        needle = "\n" + PROMPT_MARKER
        idx = full_text.rfind(needle)
        if idx < 0:
            if full_text.startswith(PROMPT_MARKER):
                view.insert(edit, 0, text)
            return
        view.insert(edit, idx + 1, text)


class ClaudeSessionStartCommand(sublime_plugin.WindowCommand):
    def run(self):
        cmd = resolve_claude_cmd()
        if not (os.path.isabs(cmd[0]) and os.path.isfile(cmd[0])):
            sublime.error_message(
                "ClaudeSession: could not find `{}`.\n"
                "Install the Claude Code CLI or set `claude_command` in\n"
                "Preferences > Package Settings > ClaudeSession.".format(cmd[0])
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
                "ClaudeSession: could not create {}:\n{}".format(file_path, e)
            )
            return

        view = self.window.open_file(file_path)
        view.settings().set("word_wrap", True)
        view.assign_syntax(
            "Packages/ClaudeSession/ClaudeSession.sublime-syntax"
        )
        store_session_state(view, session_id, cmd, cwd, first_turn=True)
        view.settings().set("claude_session_needs_directory", True)
        SESSIONS[view.id()] = ClaudeSession(view, cmd, cwd, session_id)


class ClaudeSessionSendCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        view = self.view
        if view.settings().get("claude_session_needs_directory", False):
            raw = extract_directory_prompt(view)
            if raw is None:
                return
            path = raw.strip()
            if not path:
                return
            session = get_or_restore_session(view)
            if not session:
                sublime.status_message(
                    "ClaudeSession: no active session. Run 'ClaudeSession: "
                    "Start Session'."
                )
                return
            view.settings().set("claude_session_needs_directory", False)
            session.send(DIR_CHECK_PROMPT.format(path=path))
            return
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
                "ClaudeSession: no active session. Type <CONTINUE> to resume "
                "from this file, or run 'ClaudeSession: Start Session'."
            )
            return
        session.send(prompt)


class ClaudeSessionCancelCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            return
        session = get_or_restore_session(view)
        if session:
            session.cancel()


class ClaudeSessionStopCommand(sublime_plugin.WindowCommand):
    def run(self):
        view = self.window.active_view()
        if not view:
            return
        session = SESSIONS.pop(view.id(), None)
        if session:
            session.stop()


class ClaudeSessionListener(sublime_plugin.EventListener):
    def on_load(self, view):
        self._maybe_activate(view)

    def on_activated(self, view):
        self._maybe_activate(view)

    def _maybe_activate(self, view):
        s = view.settings()
        if s.get("claude_session_active"):
            return
        fname = view.file_name() or ""
        if SESSION_FILENAME_RE.search(fname):
            s.set("claude_session_active", True)
            view.assign_syntax(
                "Packages/ClaudeSession/ClaudeSession.sublime-syntax"
            )

    def on_close(self, view):
        session = SESSIONS.pop(view.id(), None)
        if session:
            session.stop()
