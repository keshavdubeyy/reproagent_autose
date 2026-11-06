Title: CLI breaks when `--desc` contains a hyphen

Using the command-line interface, e.g. `seq 10 | python -m tqdm --desc "Test CLI-delims" --ascii`, fails:
the part after the hyphen inside the description value is treated as a new option, so tqdm complains about an
unknown option / mis-parses the arguments. Hyphens inside an option value (not preceded by a space) should be
kept as part of the value. The CLI entry point is `tqdm.main()` reading `sys.argv`.
