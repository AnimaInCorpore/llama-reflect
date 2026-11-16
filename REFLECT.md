# Working on Reflect

Keep the coding loop small and use `llama-server` for model requests. Keep terminal output readable in PowerShell, MSYS2, Linux, and macOS.

Use a disposable project folder for tool and reflection tests. Check the actual file result and the saved session tree. When investigating a stall, look for a repeatable action that advances the expected result. Try a saved project skill when it applies.

Acceptance checks:

- Run `python -m unittest` after every change. The tests need no server.
- Run `python bench/reflection_ab.py --validate` after changing a benchmark task or the stall detector.
- Keep the README, `docs/design.md`, and `docs/benchmark-plan.md` in line with the behavior and results.

Record reproducible workflows in `.refl/skills/`. Keep context such as the goal, commands, and acceptance checks here when they apply across sessions.
