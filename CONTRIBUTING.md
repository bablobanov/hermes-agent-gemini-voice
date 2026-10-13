# Contributing

Changes go through pull requests. Keep the plugin on public Hermes surfaces only: no core patches, no
monkeypatching, no self-updating code, no extra approvals of its own and none bypassed. Discuss larger
features in an issue first.

Run the unit tests from the repository root and add a regression test for every behaviour change:

```sh
python -B -m unittest discover -s plugins/gemini-voice/tests -t plugins/gemini-voice/tests -v
```

Use synthetic IDs and test values only; never paste real tokens, client secrets, document contents or logs.
Report skipped tests (for example the optional engine contract test) separately from passes.

The owner-only boundary in `sources.py` and the at-most-once journal in `journal.py` / `bridge.py` are the
security core: changes there need a test for the failure they prevent.

Keep README.md, README.zh-Hans.md and README.ru.md in sync, update CHANGELOG.md, keep the plugin free of
third-party Python dependencies. By contributing you agree to license your contribution under the MIT license.
