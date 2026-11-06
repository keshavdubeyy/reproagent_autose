# ADR-0003: Temp-copy sandbox instead of Docker
**Status:** Accepted (revisit for final)

**Context:** Docker is not available on all team machines. The prototype targets pure-Python projects.

**Decision:** Copy the repo into a temp directory and confine all paths to it. Run pytest as a subprocess with a timeout and a scrubbed environment.

**Consequences:** Simple and fast. It does not isolate the network or the filesystem from a malicious test, so it is acceptable only for curated benchmark repos. Docker isolation is future work.
