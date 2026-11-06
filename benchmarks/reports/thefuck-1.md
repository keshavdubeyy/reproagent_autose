Title: Crash when trying to correct `pip un+install thefuck`

Steps: run `pip un+install thefuck`, pip answers
`ERROR: unknown command "un+install" - maybe you meant "uninstall"`, then run `fuck`.
Expected: the suggestion `pip uninstall thefuck`. Actual: nothing useful / the rule crashes.
The rule involved is `thefuck.rules.pip_unknown_command` (`get_new_command`). Simple cases like
`pip instatl` -> `pip install` already work.
