# sublime-claude-session

A Sublime Text plugin that runs an interactive [Claude Code](https://docs.claude.com/en/docs/claude-code) session inside a Sublime buffer. You type a prompt, press `Ctrl+Enter`, and Claude's reply is streamed back into the same view. Multi-turn conversation state is preserved across sends via Claude Code's `--session-id` / `--resume`.

## Requirements

- Sublime Text 4 (Python 3.8 plugin host).
- The `claude` CLI from Claude Code, installed and runnable from the shell.

## Installation

1. Open Sublime's packages folder: `Preferences > Browse Packages…`.
2. Copy or symlink this directory into that folder as `ClaudeSession`:
   ```
   ln -s /Users/momcilo/Development/PytonProject/sublime-claude-session \
         "$HOME/Library/Application Support/Sublime Text/Packages/ClaudeSession"
   ```
3. Restart Sublime Text.

## Usage

1. Open the Command Palette (`Ctrl/Cmd+Shift+P`) and run **ClaudeSession: Start Session**. The plugin creates `ClaudeSession-<uuid>.txt` in the session's working directory and opens it as a regular text file with a banner.
2. The first interactive line is a pre-filled directory prompt: `-> Enter working directory(current - if you want to work from here just hit CTRL + ENTER): <cwd>`. Edit the path to point Claude at a different folder, or press `Ctrl+Enter` to accept the current one. The plugin sends Claude a short check-and-cd instruction; Claude either confirms the folder exists and switches into it, or tells you it doesn't and does nothing.
3. From the second turn onward, prompts use a `> ` marker. Type below it, press `Ctrl+Enter` to send. Claude's response streams in below your prompt; when it finishes, a new `> ` marker appears for the next turn.
4. The buffer is a normal file — `Ctrl/Cmd+S` saves the transcript at any time, and closing a dirty view prompts to save. Closing the view also ends the session and terminates any in-flight `claude` process; the `.txt` file stays on disk.

The session's working directory is picked, in order, from: the first open folder in the window, the directory of the active file, or `$HOME`. That's also where `ClaudeSession-<uuid>.txt` is written.

## Commands

| Palette caption | Command id | Notes |
|---|---|---|
| ClaudeSession: Start Session | `claude_session_start` | Opens a new session view. |
| ClaudeSession: Send Prompt | `claude_session_send` | Sends text after the current prompt marker. Bound to `Ctrl+Enter` in session views. |
| ClaudeSession: Cancel Request | `claude_session_cancel` | Terminates the in-flight `claude` process; keeps the session. Bound to `Ctrl+.` in session views. |
| ClaudeSession: Stop Session | `claude_session_stop` | Ends the session and discards state. |

## Key bindings

`Ctrl+Enter` (send) and `Ctrl+.` (interrupt the running turn — same effect as `Ctrl+C` in the CLI) are bound only inside a ClaudeSession session view (gated by the `claude_session_active` view setting), so they won't shadow your global bindings elsewhere.

## Settings

`Preferences > Package Settings > ClaudeSession > Settings`:

```json
{
    "claude_command": ["claude"]
}
```

`claude_command` is the argv used to launch the CLI. Use it to point at a non-PATH binary or to pin flags, e.g.:

```json
{ "claude_command": ["/opt/homebrew/bin/claude", "--model", "claude-opus-4-7"] }
```

If `claude` isn't on `PATH`, the plugin also probes these fallbacks before failing:

- `/opt/homebrew/bin/claude`
- `/usr/local/bin/claude`
- `~/.claude/local/claude`
- `~/.local/bin/claude`
- `~/.npm-global/bin/claude`
- `~/.volta/bin/claude`

## How it works

Each session generates a UUID and invokes `claude --print` once with `--session-id <uuid>` for the first turn, then `--resume <uuid>` for every subsequent turn. Output is read from the subprocess on a background thread and appended to the view via `sublime.set_timeout`. The buffer position right after the last `> ` marker is the *prompt anchor* — `claude_session_send` sends everything between the anchor and end-of-buffer.

## Troubleshooting

- **"could not find `claude`"** — install the CLI or set an absolute path in `claude_command`.
- **No response / hung session** — run *ClaudeSession: Cancel Request*, then check Sublime's console (`` Ctrl+` ``) for the `[ClaudeSession] cwd=… args=…` log line and any subprocess error.
- **`Ctrl+Enter` doesn't send** — make sure you're in the *Claude Session* view; the binding is scoped to it.
