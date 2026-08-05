#!/usr/bin/env bash
set -u

cd /app/web

if npx vitest run lib/paperpal/__tests__/hint.test.ts; then
    tests_passed=1
else
    tests_passed=0
fi

printf '{"tests_passed": %s}\n' "${tests_passed}" \
    > /logs/verifier/reward.json
