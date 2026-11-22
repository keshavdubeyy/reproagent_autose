"""Ablation table for the paper, straight from the run summaries.

    python -m autose.evaluation.tables runs/night1_A runs/night1_C runs/night2_D runs/night2_E

Writes results/ablation.md and results/ablation.tex. Only bugs present in every run are compared, so
the configurations are measured on the same bugs. "vs first" counts bugs resolved by this configuration
and not by the first one (wins) and the other way round (losses).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def rows_of(d: Path) -> dict[str, dict]:
    out = {}
    for line in (d / "summary.jsonl").read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out[r["instance_id"]] = r
    return out


def name_of(d: Path) -> str:
    cfg = d / "config.json"
    if cfg.exists():
        c = json.loads(cfg.read_text())
        if "config" in c:
            return c["config"]
    return d.name


def clean_p2p(r: dict) -> bool:
    p = r.get("p2p") or ""
    return "/" in p and p.split("/")[0] == p.split("/")[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, default=Path("results/ablation"))
    a = ap.parse_args()
    runs = [(name_of(d), rows_of(d)) for d in a.runs]
    common = sorted(set.intersection(*(set(r) for _, r in runs)))
    n = len(common)
    first = runs[0][1]
    table = []
    for name, rows in runs:
        rs = [rows[i] for i in common]
        resolved = {r["instance_id"] for r in rs if r.get("resolved")}
        base = {i for i in common if first[i].get("resolved")}
        table.append({
            "config": name, "n": n,
            "reproduced": sum(r.get("verdict") == "reproduced" for r in rs),
            "valid_repro": sum(bool(r.get("fail_to_pass")) for r in rs),
            "patch_accepted": sum(r.get("fix_status") == "fixed" for r in rs),
            "p2p_clean": sum(clean_p2p(r) for r in rs if r.get("fix_status") == "fixed"),
            "resolved": len(resolved),
            "wins": len(resolved - base), "losses": len(base - resolved),
            "min_per_bug": round(sum(r.get("wall_s", 0) for r in rs) / max(n, 1) / 60, 1),
            "llm_calls": round(sum((r.get("llm_calls") or 0) + (r.get("fix_llm_calls") or 0) for r in rs) / max(n, 1), 1),
            "errors": sum("error" in r for r in rs),
        })
    pc = lambda x: f"{x} ({100 * x / n:.0f}%)" if n else "–"
    md = ["| Config | Reproduced | Valid repro | Patch accepted | No regressions | **Resolved** | vs first (+/−) | min/bug | LLM calls | errors |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    tex = [r"\begin{tabular}{lrrrrrr}", r"\toprule",
           r"Config & Reproduced & Valid repro & Patch acc. & Resolved & $\pm$ vs A & min/bug \\", r"\midrule"]
    for t in table:
        md.append(f"| {t['config']} | {pc(t['reproduced'])} | {pc(t['valid_repro'])} | {pc(t['patch_accepted'])} | "
                  f"{t['p2p_clean']}/{t['patch_accepted']} | **{pc(t['resolved'])}** | +{t['wins']} / −{t['losses']} | "
                  f"{t['min_per_bug']} | {t['llm_calls']} | {t['errors']} |")
        tex.append(f"{t['config']} & {t['reproduced']} & {t['valid_repro']} & {t['patch_accepted']} & "
                   f"{t['resolved']} & +{t['wins']}/$-${t['losses']} & {t['min_per_bug']} \\\\")
    tex += [r"\bottomrule", r"\end{tabular}"]
    note = f"\n{n} bugs present in every run ({', '.join(str(d) for d in a.runs)}).\n"
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.with_suffix(".md").write_text("\n".join(md) + "\n" + note, encoding="utf-8")
    a.out.with_suffix(".tex").write_text("\n".join(tex) + "\n", encoding="utf-8")
    a.out.with_suffix(".json").write_text(json.dumps(table, indent=1), encoding="utf-8")
    print("\n".join(md) + note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
