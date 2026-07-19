# Bug report

Our batch jobs draw a progress bar on the console while they run (and the same
output ends up in our nightly log files). Some of those jobs know up front how
many items they are going to work through; others are streaming and genuinely do
not know until they are done.

Since we picked up the latest version, the little piece of status text that sits
beside the bar is showing the wrong one of its two forms. The two cases look like
they have traded places:

- Jobs that DO know how many items are coming used to show a live figure beside
  the bar that climbed as the job went along. That slot is now simply empty.
- Jobs that DON'T know used to leave that slot empty. They now show a figure that
  is pinned at zero for the whole run and never budges — which reads to our
  on-call as a hung job.

Nothing raises, there is no traceback, and the bar itself still animates normally.
Everything else drawn beside the bar looks right. It is only that one piece of
status text, and only *which* of its two forms it picks.

Can you find what is causing this and fix it? I do not know where in the codebase
this lives — I only see the symptom.

When you believe you have found the cause, emit exactly one line:
LOCATED: {"file": "...", "symbol": "...", "lines": [start, end]}
Then fix it and make the test suite pass.
