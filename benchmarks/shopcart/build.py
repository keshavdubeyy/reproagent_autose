"""Materialise ShopCart bugs as work/<id>/{buggy,fixed} trees from app/ + bugs.json."""
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    bugs = json.loads((HERE / "bugs.json").read_text(encoding="utf-8"))["bugs"]
    for b in bugs:
        out = HERE / "work" / b["id"]
        if out.exists():
            shutil.rmtree(out)
        ign = shutil.ignore_patterns("__pycache__")
        shutil.copytree(HERE / "app", out / "fixed", ignore=ign)
        shutil.copytree(HERE / "app", out / "buggy", ignore=ign)
        f = out / "buggy" / b["file"]
        src = f.read_text(encoding="utf-8")
        assert b["old"] in src, f"{b['id']}: mutation anchor not found"
        f.write_text(src.replace(b["old"], b["new"], 1), encoding="utf-8")
        print("built", b["id"])


if __name__ == "__main__":
    main()
