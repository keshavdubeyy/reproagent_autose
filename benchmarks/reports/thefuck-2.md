Title: Executables lookup splits PATH on ':' — broken on Windows

`thefuck.utils.get_all_executables()` scans every directory in the PATH environment variable. On Windows PATH
looks like `C:\foo;C:\bar` (separator `;`), but the directories are not split correctly, so the wrong paths are
scanned and commands are not found. PATH should be split using the platform's path separator.
