# Toolbench complex probes as Harbor tasks

This directory turns selected Toolbench complex probes into tasks that Harbor
can build and run.

## First task: WIDS D2

`wids-D2-string-keyed-dispatch` gives an agent a small, real debugging problem:
a "Get a hint" request uses the wrong endpoint string. The task container:

1. clones the WIDS repository at commit `a39cdd0`;
2. applies the saved D2 defect patch;
3. installs the web application's Node dependencies;
4. removes the original Git history so the answer cannot be looked up; and
5. runs without container networking.

The network setup has two layers:

- `task.toml` uses Harbor's portable `public` policy because Docker Desktop for
  macOS cannot provide Harbor's Linux-only egress controller.
- `environment/docker-compose.yaml` sets the task's `main` container to
  `network_mode: none`, which still removes its network access on macOS.

## Step 1: verify that the environment builds

From the repository root, run:

```bash
harbor run \
  -p benchmarks/harbor/toolbench-complex/wids-D2-string-keyed-dispatch \
  -a nop \
  --install-only \
  -n 1 \
  --job-name toolbench-wids-d2-build-canary \
  --jobs-dir reports/harbor-jobs
```

Think of `nop` as a pretend agent that does nothing. `--install-only` asks
Harbor to build and prepare the environment, then stop before solving or
grading the task.

Step 1 passes when the job's `result.json` reports:

- `n_completed_trials: 1`
- `n_errored_trials: 0`
- a non-empty `environment_setup.finished_at`

This canary passed locally on July 24, 2026 with Harbor 0.20.0. The saved,
gitignored evidence is under
`reports/harbor-jobs/toolbench-wids-d2-build-canary-2/`.

## What is not verified yet

Step 1 proves that Harbor can build the starting environment. It does not prove
that an agent can solve the bug or that the verifier gives the correct score.
Those are the next checks:

1. run an oracle solution and expect a reward of `1`;
2. run the unchanged defective task and expect a reward of `0`;
3. run a real agent and inspect both its answer and trajectory.
