# ADR-0004: Small, SWE-agent-style tool interface
**Status:** Accepted

**Decision:** Provide few tools with bounded outputs: search results capped at 50 hits, file reads with line numbers, test output limited to its last 4 KB. Editing uses `replace_lines`, with exact-match `edit_file` as a fallback.

**Rationale:** In our first fix run, unique-string replacement failed because the buggy line (`return s[mid]`) appeared twice, and the model patched the wrong line. Line-range edits based on numbered reads fixed this.
