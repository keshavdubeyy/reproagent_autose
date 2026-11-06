# ADR-0002: The harness, not the model, decides success
**Status:** Accepted

**Context:** Small models often claim success wrongly. In early runs the model declared `needs_info` because of a tool error.

**Decision:** Each stage ends with a deterministic check: the repro test must fail for a valid reason, and the fix must pass the repro test plus the existing suite. A known-fixed version, where available, gives a fail-to-pass oracle.

**Consequences:** The results are trustworthy. A valid-looking test can still assert the wrong behaviour; the oracle catches this in evaluation but not in deployment.
