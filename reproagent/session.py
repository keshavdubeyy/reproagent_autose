"""Interactive sessions for the chat UI (see docs/product-flows.md).

A session owns one sandboxed Workspace for its whole life, so the user can pause between stages:
  understand -> (confirm) -> reproduce -> (decide) -> fix -> (apply | discard)
Everything the UI shows is an append-only list of events; the browser polls for new ones.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path

from .agent import Trace
from .llm import DEFAULT_MODEL, LLM
from .pipeline import ReproAgent, Result
from .workspace import Workspace

INTAKE_SYSTEM = """You read software bug reports. Extract a structured understanding as JSON with keys:
"title" (short), "observed" (what actually happens), "expected" (what should happen),
"steps" (inputs or steps to trigger it, one string), "entities" (list of code names mentioned: functions,
classes, modules, CLI flags), "missing" (list of facts needed to write a test that the report does NOT give; if the report already
states the expected result or names the function, do NOT list them; usually this list is empty).
Use only facts in the report; leave a field "" if unknown."""


# ---------------------------------------------------------------- workspace inspection
def detect_python(repo: Path) -> str:
    for cand in (".venv/bin/python", "venv/bin/python", "env/bin/python"):
        if (repo / cand).exists():
            return str(repo / cand)
    return sys.executable


def inspect_repo(path: str, python: str | None = None) -> dict:
    repo = Path(path).expanduser()
    if not repo.is_dir():
        return {"ok": False, "error": f"Folder not found: {path}"}
    py_files = [p for p in repo.rglob("*.py") if not any(x in p.parts for x in (".venv", "venv", ".git", "node_modules"))]
    if not py_files:
        return {"ok": False, "error": "No Python files found in this folder."}
    tests = [p for p in py_files if p.name.startswith("test") or "tests" in p.parts]
    branch = ""
    try:
        branch = subprocess.run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    python = python or detect_python(repo)
    ws = Workspace(repo, python)
    try:
        healthy, out = ws.preflight()
    finally:
        ws.cleanup()
    return {"ok": True, "path": str(repo.resolve()), "name": repo.resolve().name, "py_files": len(py_files),
            "test_files": len(tests), "branch": branch, "python": python, "healthy": healthy,
            "health_output": "" if healthy else out[-1200:]}


def fetch_github_issue(url: str) -> dict:
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)", url.strip())
    if not m:
        raise ValueError("Not a GitHub issue URL (expected github.com/<owner>/<repo>/issues/<n>)")
    api = f"https://api.github.com/repos/{m[1]}/{m[2]}/issues/{m[3]}"
    req = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": "reproagent"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read())
    return {"title": d.get("title", ""), "body": d.get("body") or "", "url": url}


# ---------------------------------------------------------------- session
class Session:
    def __init__(self, store: Path, workspace: dict, model: str = DEFAULT_MODEL, bug_id: str | None = None,
                 fixed_repo: str | None = None, gt_test: str | None = None):
        self.id = "S-" + uuid.uuid4().hex[:4].upper()
        self.store = store
        self.workspace = workspace
        self.model = model
        self.bug_id = bug_id
        self.fixed_repo, self.gt_test = fixed_repo, gt_test
        self.title = "New session"
        self.state = "awaiting_report"
        self.stage = "understand"
        self.events: list[dict] = []
        self.report = ""
        self.understanding: dict = {}
        self.ws: Workspace | None = None
        self.res: Result | None = None
        self.agent: ReproAgent | None = None
        self.busy = False
        self.created = time.time()
        self.lock = threading.Lock()
        self.emit("card", kind="workspace", data=workspace)

    # ---- events -------------------------------------------------------
    def emit(self, type_: str, **kw):
        with self.lock:
            self.events.append({"i": len(self.events), "type": type_, "t": round(time.time(), 2), **kw})
        self.save()

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "state": self.state, "stage": self.stage, "busy": self.busy,
                "repo": self.workspace.get("name"), "created": self.created}

    def save(self):
        self.store.mkdir(parents=True, exist_ok=True)
        data = {**self.summary(), "workspace": self.workspace, "events": self.events, "report": self.report,
                "understanding": self.understanding}
        (self.store / f"{self.id}.json").write_text(json.dumps(data, indent=1, default=str))

    def _set(self, state: str, stage: str | None = None):
        self.state = state
        if stage:
            self.stage = stage
        self.emit("state", state=state, stage=self.stage)

    def _guard(self):
        if self.busy:
            raise RuntimeError("The agent is still working on this session.")

    def _bg(self, fn, *a):
        self.busy = True  # set before the thread starts, so a double click cannot start a second run
        def run():
            try:
                fn(*a)
            except Exception as e:  # never leave the UI hanging
                self.emit("card", kind="error", data={"message": f"{type(e).__name__}: {e}"})
                self._set("failed")
            finally:
                self.busy = False
                self.save()
        threading.Thread(target=run, daemon=True).start()

    # ---- flow 2: intake --------------------------------------------------
    def submit_report(self, text: str):
        self._guard()
        if self.state in ("needs_info", "not_reproduced") and self.report:
            self.emit("user", text=text)
            self.report += "\n\nAdditional details from the user:\n" + text
            return self._bg(self._reproduce)
        self.report = text
        self.emit("user", text=text)
        self._bg(self._understand)

    def _understand(self):
        self._set("understanding", "understand")
        llm = LLM(self.model)
        msg = llm.chat([{"role": "system", "content": INTAKE_SYSTEM}, {"role": "user", "content": self.report}],
                       format="json")
        try:
            u = json.loads(msg.get("content") or "{}")
        except json.JSONDecodeError:
            u = {}
        u = {k: u.get(k, "" if k not in ("entities", "missing") else []) for k in
             ("title", "observed", "expected", "steps", "entities", "missing")}
        self.understanding = u
        self.title = (u.get("title") or self.report.splitlines()[0])[:80]
        self.emit("card", kind="understanding", data=u)
        self._set("awaiting_confirm")

    def confirm(self, edited: dict | None = None):
        self._guard()
        if self.state != "awaiting_confirm":
            raise RuntimeError("Nothing to confirm right now.")
        if edited:
            self.understanding.update({k: v for k, v in edited.items() if k in self.understanding})
            self.title = self.understanding.get("title") or self.title
        self.emit("user", text="Looks right. Reproduce it.")
        self._bg(self._reproduce)

    def _agent_report(self) -> str:
        u = self.understanding
        parts = [self.report]
        if u:
            parts.append("\nStructured understanding (confirmed by the user):")
            for k in ("observed", "expected", "steps"):
                if u.get(k):
                    parts.append(f"- {k}: {u[k]}")
            if u.get("entities"):
                parts.append("- code mentioned: " + ", ".join(map(str, u["entities"])))
        return "\n".join(parts)

    # ---- flow 3: reproduce ---------------------------------------------
    def _reproduce(self):
        self._set("reproducing", "reproduce")
        if self.ws is None:
            self.ws = Workspace(self.workspace["path"], self.workspace.get("python"))
        self.agent = ReproAgent(LLM(self.model), "full")
        self.res = Result(bug_id=self.bug_id or self.workspace.get("name", "repo"), mode="full")
        trace = Trace(on_step=lambda s: self.emit("step", step=s))
        trace.add("stage", name="reproduce")
        ok, out = self.ws.preflight()
        if not ok:
            self.emit("card", kind="env_error", data={"output": out})
            return self._set("env_error")
        self.agent._reproduce(self.ws, self._agent_report(), self.res, trace)
        r = self.res
        if r.repro_valid and self.fixed_repo:
            r.fail_to_pass = ReproAgent._oracle(self.fixed_repo, self.ws.test_path(), r.test_code,
                                                self.workspace.get("python"))
        data = {"test_code": r.test_code, "evidence": r.evidence, "attempts": r.repro_attempts,
                "explanation": r.explanation, "test_path": self.ws.test_path(), "fail_to_pass": r.fail_to_pass}
        rej = [s["step"].get("why") for s in self.events if s["type"] == "step"
               and s["step"].get("kind") == "verify_repro" and not s["step"].get("ok")]
        data["rejections"] = rej
        if r.status == "reproduced":
            self.emit("card", kind="reproduced", data=data)
            self._set("awaiting_fix_decision", "verify_failure")
        elif r.status == "needs_info":
            self.emit("card", kind="needs_info", data=data)
            self._set("needs_info")
        else:
            self.emit("card", kind="not_reproduced", data=data)
            self._set("not_reproduced")

    # ---- flow 4: fix ------------------------------------------------------
    def fix(self, hint: str = ""):
        self._guard()
        if not (self.res and self.res.repro_valid):
            raise RuntimeError("Nothing reproduced yet.")
        self.emit("user", text="Fix it." + (f" Hint: {hint}" if hint else ""))
        self._bg(self._fix, hint)

    def _fix(self, hint: str):
        self._set("fixing", "fix")
        self.ws.revert()
        r = self.res
        r.fix_status, r.fix_attempts, r.patch = "skipped", 0, ""
        trace = Trace(on_step=lambda s: self.emit("step", step=s))
        trace.add("stage", name="fix")
        report = self._agent_report() + (f"\n\nHint from the developer: {hint}" if hint else "")
        self.agent._fix(self.ws, report, r, trace)
        if r.fix_status == "fixed" and self.fixed_repo and self.gt_test:
            r.gt_pass = ReproAgent._gt_check(self.ws, self.fixed_repo, self.gt_test)
            # _gt_check copies the developer test into the sandbox; it is evaluation-only, not part of the patch
        data = {"patch": r.patch, "attempts": r.fix_attempts, "regression_free": r.regression_free,
                "explanation": r.explanation.split("Fix:")[-1].strip() if "Fix:" in r.explanation else "",
                "files": list(self.ws.original), "gt_pass": r.gt_pass,
                "lines": sum(1 for l in r.patch.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))}
        if r.fix_status == "fixed":
            self.emit("card", kind="fixed", data=data)
            self._set("awaiting_apply", "verify_fix")
        else:
            self.emit("card", kind="not_fixed", data=data)
            self._set("not_fixed", "fix")

    # ---- apply / discard --------------------------------------------------
    def apply(self, include_patch: bool = True) -> dict:
        """Write the verified patch (and the reproduction test) into the user's real repository."""
        self._guard()
        if include_patch and self.state != "awaiting_apply":
            raise RuntimeError("There is no verified fix to apply.")
        if not (self.ws and self.res and self.res.repro_valid):
            raise RuntimeError("Nothing to apply.")
        repo = Path(self.workspace["path"])
        written = []
        if include_patch:
            for rel, before in self.ws.original.items():
                target = repo / rel
                if target.read_text() != before:
                    raise RuntimeError(f"{rel} changed in your repository since the session started; not applied.")
            for rel in self.ws.original:
                (repo / rel).write_text((self.ws.root / rel).read_text())
                written.append(rel)
        test_rel = self.ws.test_path()
        dest = repo / test_rel
        if dest.exists() and dest.read_text() != self.res.test_code:
            dest = dest.with_name(f"test_repro_{self.id.lower().replace('-', '_')}.py")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.res.test_code)
        written.append(str(dest.relative_to(repo)))
        self.emit("user", text="Apply to repository." if include_patch else "Save the test to my repository.")
        self.emit("card", kind="applied", data={"files": written, "path": str(repo)})
        self._set("applied" if include_patch else "test_saved", "done")
        return {"files": written}

    def discard(self):
        self._guard()
        self.emit("user", text="Discard.")
        if self.ws:
            self.ws.cleanup()
            self.ws = None
        self.emit("agent", text="Discarded. Your repository was not touched.")
        self._set("discarded", "done")


WORKING_STATES = ("understanding", "reproducing", "fixing")


def _archived_state(state: str | None) -> str | None:
    """A session left mid-run by a server restart cannot continue; say so instead of showing a spinner."""
    return "interrupted" if state in WORKING_STATES else state


class SessionStore:
    def __init__(self, root: Path):
        self.root = root
        self.live: dict[str, Session] = {}

    def create(self, **kw) -> Session:
        s = Session(self.root, **kw)
        self.live[s.id] = s
        return s

    def get(self, sid: str) -> Session | None:
        return self.live.get(sid)

    def list(self) -> list[dict]:
        out = {s.id: s.summary() for s in self.live.values()}
        if self.root.exists():
            for p in self.root.glob("S-*.json"):
                if p.stem not in out:
                    try:
                        d = json.loads(p.read_text())
                        out[p.stem] = {k: d.get(k) for k in ("id", "title", "state", "stage", "repo", "created")} | {
                            "busy": False, "archived": True, "state": _archived_state(d.get("state"))}
                    except (json.JSONDecodeError, OSError):
                        pass
        return sorted(out.values(), key=lambda x: -(x.get("created") or 0))

    def events(self, sid: str, since: int) -> dict | None:
        s = self.live.get(sid)
        if s:
            return {**s.summary(), "workspace": s.workspace, "understanding": s.understanding,
                    "events": s.events[since:], "n": len(s.events)}
        p = self.root / f"{sid}.json"
        if p.exists():
            d = json.loads(p.read_text())
            return {**{k: d.get(k) for k in ("id", "title", "stage", "repo")}, "state": _archived_state(d.get("state")),
                    "busy": False, "archived": True,
                    "workspace": d.get("workspace"), "understanding": d.get("understanding"),
                    "events": d["events"][since:], "n": len(d["events"])}
        return None

