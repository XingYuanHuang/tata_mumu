# TataFishing 0.2.3 - test record

This patch removes policy-style restrictions that interfered with using higher
multipliers while retaining state-machine checks needed for correct QTE/hook
sequencing.

## User-reported x10 regression

The supplied x10 screenshot was cropped through the same automatic locator used
by the application and saved as `tests/fixtures/x10_ready.png`.

Validated behavior:

- it is recognized as `READY`;
- its old x1 template score is below the former 0.87 gate;
- the simulated worker still sends CAST and completes a full low-grade
  cast -> hook -> result cycle;
- the run ends by the selected cast limit, not `multiplier`.

## Restrictions removed in 0.2.3

- no x1-only check before CAST;
- a changed resource/consumable counter no longer blocks the compatibility
  CAST retry;
- no automatic one-hour stop;
- long WAIT / TRANSITION / temporarily UNKNOWN screens keep being observed
  instead of stopping solely because of elapsed time.

F9 and the user-selected cast count remain the explicit stop controls. Input
failures, QTE state errors and lost/moved windows can still stop because they
indicate that the controller no longer knows where or when to send the intended
input.

## Tests run

- 85 controller/locator/worker/hook/QTE/result regression tests: PASS
- 14 motion/vision tests: PASS
- 5 result-worker integration tests: PASS
- headless GUI smoke test: PASS
- Python compileall: PASS

Total selected unit/integration tests: 104 PASS, plus GUI smoke and compile check.

These are offline screenshot and simulated-input tests. They do not claim a live
Windows/MuMu acceptance test or account-safety guarantee.
