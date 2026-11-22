# ADR 0010: Everything runs on one laptop

Status: accepted (22 Nov 2026)

## Context
All runs happen on one MacBook (Apple M4, 16 GB RAM, ~35 GB free disk, Docker VM 8 GB / 10 CPUs).
One SWE-bench image is ~4 GB; one bug × configuration takes ~3-6 minutes under emulation.

## Decision
- Bug-major order: pull one image, run every configuration on it, grade, delete the image. Bugs are sorted
  by (repo, version) so shared environment layers stay cached.
- A fixed 40-bug subset from few environments for the agent runs (~160 runs = two nights). Localisation is
  measured on every downloaded bug because it needs neither a model nor Docker.
- One model loaded at a time, `think` off for qwen3, 180 s timeout per test run.

## Consequences
- Peak disk use stays around 5-6 GB for images.
- N = 40 limits statistical power; we report confidence intervals and a 3-seed variance check on 10 bugs.
