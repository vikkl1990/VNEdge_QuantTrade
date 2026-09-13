#!/usr/bin/env python3
"""CI test gate: run the suite, fail only on a NEW regression.

(2026-09-13) 16 pre-existing test failures sat uninvestigated long enough
this session that telling drift from a real regression took deliberate
archaeology (git stash + diff the failure list against HEAD). That's what a
CI job is for. But with zero CI ever having run here, marking all 16 as
xfail right now would require deciding, test by test, whether each is
"known, ignore forever" or "should actually be fixed" -- a judgment call
that belongs to whoever owns each test, not to a first CI pass.

So: ratchet, not xfail. BASELINE_FAILURES is the count on this commit; CI
is green today at exactly that count, red if it goes up (a new regression
landed), and asks a human to lower BASELINE_FAILURES if it legitimately
goes down (someone fixed one). Update the number by hand when the known
failures change -- that's the point: a failure count changing is always
worth a look, in either direction.
"""
import re
import subprocess
import sys

BASELINE_FAILURES = 16

PYTEST_ARGS = [
    "-m", "pytest", "tests",
    "--ignore=tests/smoke_test_live.py",
    "--ignore=tests/load",
    "--asyncio-mode=auto",
    "-q", "--no-header", "-p", "no:cacheprovider",
]


def main() -> int:
    proc = subprocess.run(
        [sys.executable] + PYTEST_ARGS,
        capture_output=True, text=True,
    )
    output = proc.stdout + proc.stderr
    print(output)

    m = re.search(r"(\d+) failed", output)
    failed = int(m.group(1)) if m else 0
    passed_m = re.search(r"(\d+) passed", output)
    passed = int(passed_m.group(1)) if passed_m else 0

    if failed > BASELINE_FAILURES:
        print(
            f"\nGATE FAILED: {failed} tests failed, more than the baseline "
            f"of {BASELINE_FAILURES}. This looks like a new regression, not "
            f"known drift -- check the diff against the last green run "
            f"before assuming it's pre-existing.",
            file=sys.stderr,
        )
        return 1

    if failed < BASELINE_FAILURES:
        print(
            f"\nGATE NOTE: only {failed} tests failed (baseline was "
            f"{BASELINE_FAILURES}) -- looks like something got fixed. "
            f"Lower BASELINE_FAILURES in scripts/ci_test_gate.py to match, "
            f"so a real future regression doesn't hide under the old count.",
        )

    print(f"\nGATE OK: {passed} passed, {failed} failed (baseline {BASELINE_FAILURES}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
