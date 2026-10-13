# TataFishing 0.2.3 - unrestricted multiplier tool mode

Extract the whole folder and double-click `run.bat`.

## What changed

0.2.3 removes the x1-only policy from 0.2.2. A READY fishing screen can cast
regardless of the multiplier shown by the game (x1, x10, or another value).
The x1 template may still be measured as diagnostic metadata, but it is never a
gate for CAST input.

The consumable/resource badge changing no longer blocks the one compatibility
retry for a CAST. This matters at higher multipliers because a legitimate cast
can change the displayed count by more than one. The one-hour automatic stop is
also removed; the selected cast-count limit and F8/F9 remain the user controls.

Result-card paging, hook recognition, QTE prediction, automatic window location,
and the input backends are unchanged from 0.2.2. Technical stale-input guards
remain so an old hook/result action is not fired into a newly visible QTE.

## Use

- Set any multiplier you want in the game before starting.
- Keep MuMu visible in the foreground.
- Pick single-fish or continuous mode and press Start.
- F8 toggles run/pause; F9 stops immediately.

The helper uses ordinary foreground desktop input. It does not read or modify
game memory and does not provide anti-cheat bypass guarantees.

## Validation

Run:

    python -m unittest discover -s tests -p "test_*.py" -v

The test suite includes a fixture made from the supplied x10 screenshot and a
simulated full fishing cycle that confirms CAST is allowed when the old x1
score is below the previous threshold. Tests are offline/simulated, not a live
MuMu acceptance test.
