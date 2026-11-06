# ADR-0005: Extend from reproduction to reproduction + fix
**Status:** Accepted (response to proposal review)

**Context:** Reviewer feedback: "make the harness also fix the bug. Just locating a bug might not require a harness-level complexity."

**Decision:** Add a Fix stage that uses the verified reproduction test as its acceptance criterion: a test-first repair. Between failed attempts the source is reverted and the harness feedback is passed on.

**Consequences:**
- **New metrics:** fix rate, regression-free rate, and fix attempts.
- **Hypotheses:** we keep H1 and H2 and add **H3:** a fix guided by a verified reproduction test succeeds more often than a fix attempted from the report alone.
- **Scope:** automatic fixing moves from out of scope to in scope.
