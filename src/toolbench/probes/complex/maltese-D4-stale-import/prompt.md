Our command-line entry point — the one you launch from a terminal to kick off an automated pass over a crate — will not even start up any more.

The quick way I check that it is installed is to invoke it bare, with nothing typed after the program name. It used to answer that by naming the one mandatory option I had left off, then exiting with a failure status. It no longer gets that far. The process now dies on the spot and dumps an uncaught-exception stack trace to the terminal: the runtime reports that it cannot locate a module the program loads on the way up, and the path it prints for that module is not a path that exists on disk.

Supplying options changes nothing. A complete, valid set of them blows up in exactly the same way, just as instantly — so it is plainly dying before it ever inspects what I typed.

And nothing at all happens beforehand: no scratch checkout is made, no output directory appears, no log is written. It never reaches the point of doing any real work.

Nobody remembers touching how options are read, and this used to work.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
