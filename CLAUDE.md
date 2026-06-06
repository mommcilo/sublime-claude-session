# CLAUDE.md

Context for future Claude Code sessions working in this repo.

## What this is

A Sublime Text 4 plugin (`ClaudeSession`) that embeds an interactive Claude Code CLI session inside a regular Sublime text-file buffer. On *Start Session* the plugin pre-writes `<cwd>/ClaudeSession-<uuid>.txt` (banner + initial `> ` marker), opens that file in a new tab, and routes streamed CLI output into it. The buffer is NOT scratch — user can save (`Ctrl/Cmd+S`) and edit freely; closing a dirty buffer triggers the normal save prompt.

Shells out to `claude --print` with session-id continuity (first turn `--session-id <uuid>`, subsequent turns `--resume <uuid>`).

Not a package distributed via Package Control yet — installed by symlinking this dir into `Packages/ClaudeSession`.

## Layout

- `claude_session.py` — the entire plugin. Single file. Contains:
  - `SESSIONS` — module-level `{view_id: ClaudeSession}` map; the source of truth for which views are session views.
  - `format_banner(session_id, cwd)` — module-level; produces the banner text that's pre-written to disk by `ClaudeSessionStartCommand` (so the file is non-empty before the view opens).
  - `ClaudeSession` — owns the subprocess, session UUID (passed in by the start command), `prompt_anchor` (buffer offset after the last `> ` marker), `busy` flag, `is_first_turn` flag.
  - `ClaudeSessionAppendCommand` (TextCommand) — the only path that writes to the buffer; everything else hops through `sublime.set_timeout` → `view.run_command("claude_session_append", ...)`.
  - `ClaudeSessionStartCommand` / `SendCommand` / `CancelCommand` / `StopCommand` — palette entry points. Start command pre-writes `<cwd>/ClaudeSession-<uuid>.txt`, opens it via `window.open_file(...)`, then polls `view.is_loading()` to set `prompt_anchor = view.size()` once the file finishes loading.
  - `ClaudeSessionListener` — `on_close` tears the session down (subprocess only; the `.txt` file persists on disk).
- `Default.sublime-commands` — palette captions.
- `Default.sublime-keymap` — `Ctrl+Enter` → `claude_session_send`, scoped to views with `claude_session_active=true`.
- `ClaudeSession.sublime-settings` — exposes `claude_command` (argv list).
- `README.md` — user-facing install + usage docs.

## Key invariants / things easy to break

- **Buffer writes must go through `claude_session_append`.** Sublime requires an `edit` token; calling `view.insert` from a worker thread will crash. `_append` already routes correctly — preserve that.
- **The session view is a real on-disk file, not scratch.** `Start Session` writes the banner to `<cwd>/ClaudeSession-<uuid>.txt` and uses `window.open_file(path)`. Do not call `set_scratch(True)` — the user expects normal save semantics (Ctrl+S, save-on-close prompts).
- **`open_file` is async.** The returned view has `is_loading() == True` briefly. The start command schedules an `anchor_when_loaded` poller (50 ms tick) that sets `prompt_anchor = view.size()` only after loading finishes. Don't try to read the buffer before then.
- **`prompt_anchor` is updated after every append.** It marks the start of the user's next prompt. `claude_session_send` reads `[prompt_anchor, view.size())` as the prompt. If you add new ways to write to the buffer, make sure the anchor advances afterward or sends will re-submit old output.
- **First turn uses `--session-id <uuid>`; later turns use `--resume <uuid>`.** Don't pass both. The `is_first_turn` flag flips inside `send()` before the subprocess starts, so a failed first turn still counts as "first turn used" — that's intentional; the session id was issued either way.
- **Streaming reads happen on a daemon thread.** All view interaction is marshalled back via `sublime.set_timeout(..., 0)` with `lambda t=text: ...` to capture loop variables. Don't drop the default-arg capture.
- **`stderr` is merged into `stdout`** (`stderr=subprocess.STDOUT`) so errors appear inline in the session view. Don't split them — the UX depends on it.
- **`view.is_valid()` guard in `_append`** handles the case where the user closes the view mid-stream. Keep it.

## Conventions

- Python 3.8 (Sublime's plugin host). No external deps; stdlib only.
- 4-space indent, double-quoted strings, no type hints (matches existing style).
- User-visible strings use `.format(...)`, not f-strings (also matches existing style).
- Debug logging goes through `print(...)` — surfaces in Sublime's console (`` Ctrl+` ``).

## Manual test loop

There's no automated test suite. To verify changes:

1. Symlink the repo into `Packages/ClaudeSession` (one-time).
2. Reload the plugin: edit & save `claude_session.py`, or `Tools > Developer > Reload Plugin`.
3. Open Sublime console (`` Ctrl+` ``) to watch the `[ClaudeSession]` log lines.
4. Run *ClaudeSession: Start Session*, send a prompt, watch it stream back.
5. Test multi-turn (does turn 2 use `--resume`?), cancel mid-stream, close-view-mid-stream.

## Known rough edges

- No way to edit/re-send a prompt; once `Ctrl+Enter` fires, the text is locked in.
- `prompt_anchor` assumes the user only appends below the last marker. If they edit higher up in the buffer while a turn is in flight, weird things can happen — but it's a single-user local tool, low priority.
- `resolve_claude_executable` fallback list is hardcoded for macOS / common Node install layouts. Linux-specific paths aren't probed.
