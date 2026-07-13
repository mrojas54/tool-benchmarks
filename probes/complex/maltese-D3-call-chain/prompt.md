The tool server the agent talks to is doing the exact opposite of what we ask it for when we bring it up.

Launch it the ordinary way — no extra options, just the plain default — and every single tool call that would change anything on disk comes straight back as an error. Creating a file, editing one, applying a diff, staging, committing: all of them refused, immediately. We never asked it to hold anything back; that is just how we start it normally.

Then launch it again, this time explicitly turning ON the cautious mode we use for dry runs — the mode whose entire job is to refuse to change anything — and those same calls all succeed, and really do change things on disk. The cautious launch is the one that changes things; the ordinary launch is the one that refuses.

Calls that only look at things behave identically and correctly under both launches: listing a directory, reading a file, running a check — all fine either way. The difference shows up only on the calls that would change something.

Nobody remembers touching how we start it, and it used to behave the right way round.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
