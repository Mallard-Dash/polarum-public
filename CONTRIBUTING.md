# Contributing to Polarum

Thanks for helping out. Polarum is small on purpose; please keep it that way.

## Ground rules

- Python 3.11+, standard library only. No pip dependencies.
- Everything user-visible and every comment is in English.
- Safety first: no state-changing action without an explicit confirmation
  step, and fail closed (stop and report) on errors.
- All SSH calls use `BatchMode=yes`. Never store, embed or log passwords or
  passphrases.
- No personal data in the repo: use placeholders such as `node-a`,
  `relay-host`, `192.168.1.x` and fake MACs like `AA:BB:CC:DD:EE:01`.
- Never run tests against real hosts. Mock `subprocess` instead.

## Workflow

1. Open an issue for larger changes before starting.
2. Keep the existing style: type hints, dataclasses, small functions,
   `from __future__ import annotations`.
3. Add `unittest` tests for pure functions (config, parsing, rules).
4. Run `python3 -m unittest discover -s tests -t .` before submitting.
5. Keep commits focused, with a clear message.
