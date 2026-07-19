A member DMed me a screenshot: they're doing the MCQ practice questions on a paper and every time they tap "Get a hint," the request just fails — the button spins for a second, then drops back to idle with a generic error toast. No hint text ever shows up.

I tried it myself in the reading-group app and got the same thing every time, on every question, on more than one paper. It's 100% reproducible, not flaky.

For comparison, the "explain this paper" synthesis feature and the Socratic follow-up-question feature both still work completely fine for the same members on the same papers, right now. So it seems isolated to just the hint path specifically, not an auth or outage problem across the board.

Nobody remembers touching anything related to hints recently, but something clearly changed since this used to work.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
