Tried to just run the detective tool with no arguments at all, the way you'd do to double check it's installed and see the usage message.

Instead of the normal quick "missing required flag" message it used to print, it now just dumps a raw crash straight to the terminal before anything else happens — no usage text, no clean exit, just a wall of an unhandled-error stack trace. This happens instantly, before it could possibly have started doing any real work (no worktree gets created, no run directory shows up, nothing).

Every other way I've tried invoking it hits the exact same immediate crash, so it doesn't seem to depend on which flags are passed — it dies before it even gets to parsing them.

Nobody remembers changing anything related to argument parsing recently.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
