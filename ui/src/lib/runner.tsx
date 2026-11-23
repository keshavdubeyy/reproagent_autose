/* Ported from the inline page in scripts/runner_ui.py. Keep the wording and the log mapping in step with it. */
import type { ReactNode } from "react"

export type Line = { t: number; s: string }
export type Spec = {
  id?: string
  target?: string
  title?: string
  body?: string
  config?: string
  fix?: boolean
  grade?: boolean
  bug_id?: string
  repo?: string
  fixed?: string | null
  budget?: number | string
}
export type Result = {
  verdict?: string
  fix_status?: string
  resolved?: boolean | null
  error?: string
  wall_s?: number | null
  llm_calls?: number | null
  attempts?: number | null
  fail_to_pass?: boolean | null
  [k: string]: unknown
}
export type Attempt = { n: number; status?: string; exception?: string; category?: string; feedback?: string; failure_line?: string; code?: string }
export type Patch = { n: number; category?: string; kind?: string; feedback?: string; diff?: string }
export type Detail = {
  missing?: boolean
  final_test?: string
  attempts?: Attempt[]
  spec?: { summary?: string; expected?: string; observed?: string; failure_kind?: string; symbols?: string[]; example_inputs?: string[] }
  fix?: { final_diff?: string; patches?: Patch[]; candidates?: string[] }
}
export type JobState = {
  id: string
  status: string
  started: number
  now: number
  root?: string
  spec: Spec
  total: number
  lines: Line[]
  result?: Result
  detail?: Detail
  error?: string
}
export type Bug = { id: string; title: string; body: string; repo?: string; image_ready?: boolean }
export type Options = { shopcart: Bug[]; swebench: Bug[]; night_running: boolean; active: string | null; root?: string }
export type HistoryRow = {
  id: string
  title?: string
  target?: string
  bug_id?: string
  config?: string
  status?: string
  verdict?: string
  fix_status?: string
  resolved?: boolean | null
  error?: string
  wall_s?: number | null
}

export const fmt = (s: number | null | undefined): string => {
  if (s == null || isNaN(s)) return ""
  s = Math.max(0, Math.round(s))
  const h = Math.floor(s / 3600),
    m = Math.floor((s % 3600) / 60),
    x = s % 60
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(x).padStart(2, "0")}s` : `${x}s`
}

export const store = {
  get(k: string): string | null {
    try {
      return localStorage.getItem(k)
    } catch {
      return null
    }
  },
  set(k: string, v: string) {
    try {
      localStorage.setItem(k, v)
    } catch {
      /* storage unavailable: keep the in-memory value only */
    }
  },
}

let ROOT = ""
export const setRoot = (r?: string) => {
  if (r) ROOT = r
}
/* Never show the absolute home path: paths inside the project become relative, anything else under a home folder becomes ~ */
export const rel = (v: unknown): string => {
  let s = String(v ?? "")
  if (ROOT) s = s.split(ROOT + "/").join("").split(ROOT).join(".")
  return s.replace(/\/(?:Users|home)\/[^/\s]+/g, "~")
}

export type TargetKey = "shopcart" | "swebench" | "folder"
export const TARGETS: Record<TargetKey, { name: string; d: string; t: string; eta: string; max: number }> = {
  shopcart: { name: "ShopCart demo", d: "A small online-shop app with planted bugs. Runs on this computer.", t: "About 2–6 min · no Docker", eta: "A ShopCart run usually takes about 2–6 minutes.", max: 360 },
  swebench: {
    name: "SWE-bench Lite",
    d: "A real bug from an open-source Python project, in its own container.",
    t: "About 5–15 min · uses Docker",
    eta: "A SWE-bench run usually takes about 5–15 minutes (longer the first time, while Docker downloads the project).",
    max: 900,
  },
  folder: { name: "Your own project", d: "Any Python project folder on this computer that has tests.", t: "Time depends on the project", eta: "Time depends on the size of the project; a few minutes is typical.", max: 900 },
}
export type CfgKey = "A" | "C" | "D" | "E"
export const CFG: Record<CfgKey, { name: string; d: string; tech: string; rec?: boolean }> = {
  A: { name: "Basic", d: "Finds code by keyword search only. The simplest version, kept for comparison.", tech: "A · baseline: keyword search, test-guided repair" },
  C: { name: "Smarter search", d: "Also matches function names and error messages to find the right code.", tech: "C · knowledge: + hybrid search (BM25, names, traceback)" },
  D: { name: "Code map", d: "Also follows which functions call which, to rank where the bug probably is.", tech: "D · code graph: + graph re-ranking, search feeds the localiser" },
  E: { name: "Full agent", d: "Everything in Code map, plus an independent review of each fix and a guard that stops it repeating a failed idea.", tech: "E · full design: D + independent review + loop controller", rec: true },
}
export const isCfg = (k: unknown): k is CfgKey => typeof k === "string" && k in CFG
export const isTarget = (k: unknown): k is TargetKey => typeof k === "string" && k in TARGETS
export const cfgName = (k?: string) => (isCfg(k) ? `${CFG[k].name} (${k})` : `setup ${k || "?"}`)

export const STAGES: [string, string][] = [
  ["Understand the report", "Reads the report: what should happen, and what happens instead."],
  ["Find the code", "Searches the project for the functions the report is about."],
  ["Show the bug with a test", "Writes a small test that should fail because of the bug, runs it, and retries if the test itself is wrong."],
  ["Find the faulty function", "Ranks the functions most likely to contain the mistake."],
  ["Write the fix", "Edits the code; up to 3 tries."],
  ["Check the fix", "Re-runs the bug's test and the project's existing tests; a reviewer double-checks the change."],
  ["Verdict", "Reports what worked. Benchmark bugs are also graded against the developers' real fix."],
]
const TAGSTAGE: Record<string, number> = { runner: 0, spec: 0, explore: 1, relax: 2, attempt: 2, localize: 3, "re-localize": 3, regression: 3, patch: 4, sweep: 5, review: 5, blackboard: 5, f2p: 6 }
export type Tone = "ok" | "bad" | "warn" | "info" | "skip" | ""
type Cat = [string, Tone, string]
export const TEST_CAT: Record<string, Cat> = {
  reproduced: ["Bug shown", "ok", "The test fails exactly the way the report describes."],
  invalid_test: ["Test had a mistake of its own", "warn", "The test itself was broken (for example it called a function the wrong way), so it never reached the bug."],
  wrong_failure: ["Failed for a different reason", "warn", "The test failed, but not in the way the report describes, so it does not prove this bug."],
  test_passed: ["Bug not triggered", "warn", "The test passed, so it did not show the bug."],
}
export const FIX_CAT: Record<string, Cat> = {
  plausible: ["Passes the checks", "ok", "The bug's test now passes and the project's existing tests still pass."],
  partial: ["Partly fixed", "warn", "Some failing checks now pass. The change is kept while the agent looks for the remaining place."],
  repro_still_fails: ["Bug still there", "bad", "After this change the bug's test still fails."],
  regression: ["Broke other tests", "bad", "The change broke existing tests that passed before."],
  not_applicable: ["Edit did not fit the code", "bad", "The edit could not be applied to the file."],
  review_rejected: ["Rejected by the reviewer", "bad", "The independent reviewer found a problem with the change."],
}
export const KIND: Record<string, string> = { wrong_value: "a wrong result", exception: "a crash or error", other: "unexpected behaviour" }
export const TICK: Record<string, string> = { ok: "✓", bad: "✕", warn: "!", info: "•", skip: "–" }

export function tagOf(s: string): string | null {
  const m = s.match(/^\[([a-z][a-z\- ]*?)(?: \d+)?\]/i)
  return m ? m[1].split(" ")[0].toLowerCase() : null
}

/* ---------- history ---------- */
export function badgeOf(r: HistoryRow): [Tone | "run", string] {
  if (r.status === "running") return ["run", "Running"]
  if (r.error === "stopped by user" || r.status === "stopped") return ["", "Stopped"]
  if (r.error) return ["bad", "Failed"]
  if (r.status === "interrupted") return ["", "Unfinished"]
  if (r.fix_status === "fixed" && r.resolved === false) return ["warn", "Fixed, hidden tests fail"]
  if (r.fix_status === "fixed") return ["ok", "Fixed"]
  if (r.verdict === "reproduced" && r.fix_status === "not_fixed") return ["warn", "Reproduced, not fixed"]
  if (r.verdict === "reproduced") return ["ok", "Reproduced"]
  if (r.verdict === "needs_more_information") return ["warn", "Needs more detail"]
  if (r.verdict === "not_reproduced") return ["bad", "Not reproduced"]
  return ["", r.status || "?"]
}
export function whenOf(id: string): string {
  const m = String(id).match(/^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)/)
  if (!m) return ""
  const d = new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])
  const t = `${m[4]}:${m[5]}`
  const today = new Date()
  if (d.toDateString() === today.toDateString()) return `Today ${t}`
  return `${d.toLocaleDateString(undefined, { day: "numeric", month: "short" })}, ${t}`
}

/* ---------- reading the log ---------- */
export type Analysis = {
  tests: string[]
  fixes: string[]
  stopRepro: boolean
  stopFix: boolean
  review: "approve" | "reject" | null
  f2p: string | null
  localized: boolean
  spec: boolean
  explore: boolean
  testOk: boolean
  fixOk: boolean
}
export function analyse(lines: Line[]): Analysis {
  const a: Analysis = { tests: [], fixes: [], stopRepro: false, stopFix: false, review: null, f2p: null, localized: false, spec: false, explore: false, testOk: false, fixOk: false }
  for (const l of lines) {
    const t = tagOf(l.s),
      s = l.s
    if (!t) continue
    let m
    if (t === "spec") a.spec = true
    else if (t === "explore") a.explore = true
    else if (t === "attempt") {
      m = s.match(/-> (\S+)/)
      a.tests.push(m ? m[1] : "?")
    } else if (t === "localize" || t === "re-localize" || t === "regression") a.localized = true
    else if (t === "patch") {
      m = s.match(/-> (\S+)/)
      a.fixes.push(m ? m[1] : "?")
    } else if (t === "review") a.review = /APPROVE/.test(s) ? "approve" : "reject"
    else if (t === "control" && /stopping/.test(s)) {
      if (a.localized) a.stopFix = true
      else a.stopRepro = true
    } else if (t === "f2p") a.f2p = s
  }
  a.testOk = a.tests.includes("reproduced")
  a.fixOk = a.fixes.includes("plausible")
  return a
}
export const hasOracle = (sp: Spec) => sp.target === "shopcart" || (sp.target === "folder" && !!sp.fixed) || (sp.target === "swebench" && !!sp.grade)

export type NowState = { st: number; head: string; sub: string; tail?: string; next?: string }
/* One plain sentence for "what is happening now" and "what comes next". */
export function nowState(a: Analysis, sp: Spec): NowState {
  const B = +(sp.budget ?? 0) || 4,
    F = 3,
    lt = a.tests[a.tests.length - 1],
    lf = a.fixes[a.fixes.length - 1]
  if (!a.spec) return { st: 0, head: "Reading the bug report", sub: "The AI model is working out what should happen and what actually happens.", next: "search the code" }
  if (!a.explore) return { st: 1, head: "Searching the code", sub: "Looking for the functions the report talks about.", next: "write a test that shows the bug" }
  if (!a.testOk && !a.stopRepro && a.tests.length < B) {
    const n = a.tests.length + 1
    return {
      st: 2,
      head: "Writing a test that shows the bug",
      tail: `attempt ${n} of up to ${B}`,
      sub: lt && TEST_CAT[lt] ? `Attempt ${n - 1}: ${TEST_CAT[lt][0].toLowerCase()}. ${TEST_CAT[lt][2]} Trying again.` : "A good test fails on the buggy code for exactly the reason the report gives.",
      next: sp.fix === false ? "report the verdict" : "find the faulty function",
    }
  }
  if (!a.testOk)
    return {
      st: 6,
      head: "Wrapping up",
      sub: a.stopRepro ? "The same failure kept repeating, so the agent stopped trying and is writing its report." : `None of the ${a.tests.length} tests showed the bug. Writing the report.`,
    }
  if (sp.fix === false) return { st: 6, head: "Wrapping up", sub: "The bug is shown. No fix was requested, so the agent is writing its report." }
  if (!a.localized) return { st: 3, head: "Finding the faulty function", sub: "The test proves the bug. Now the agent ranks functions by how likely they are to hold the mistake.", next: "write the fix" }
  if (!a.fixOk && !a.stopFix && a.fixes.length < F) {
    const n = a.fixes.length + 1
    return {
      st: 4,
      head: "Writing the fix",
      tail: `attempt ${n} of up to ${F}`,
      sub: lf && FIX_CAT[lf] ? `Fix ${n - 1}: ${FIX_CAT[lf][0].toLowerCase()}. ${FIX_CAT[lf][2]} Trying again.` : "It edits the likely faulty function, then re-runs the bug's test and the existing tests.",
      next: "check the fix",
    }
  }
  if (a.fixOk)
    return {
      st: 6,
      head: hasOracle(sp) ? "Grading the result" : "Wrapping up",
      sub: hasOracle(sp)
        ? "The fix passed its checks. Now the test is compared with the developers' real fix. This is for grading only; the agent never sees it."
        : "The fix passed its checks. Writing the report.",
    }
  return { st: 6, head: "Wrapping up", sub: "No fix passed the checks. Writing the report." }
}

export type StepState = "done" | "active" | "todo" | "fail" | "halt" | "skip"
export function trackStates(a: Analysis, s: JobState, running: boolean, liveElsewhere: boolean, ns?: NowState): StepState[] {
  if (running && ns) return STAGES.map((_, i) => (i < ns.st ? "done" : i === ns.st ? "active" : "todo"))
  const r = s.result,
    S: StepState[] = Array(7).fill("skip")
  if (!r || r.error) {
    const st = nowState(a, s.spec || {}).st,
      bad = !!(r && r.error && r.error !== "stopped by user") /* where it stopped */
    return STAGES.map((_, i) => (i < st ? "done" : i === st ? (bad ? "fail" : "halt") : "skip"))
  }
  if (a.spec) S[0] = "done"
  if (a.explore) S[1] = "done"
  if (a.tests.length) S[2] = a.testOk ? "done" : "fail"
  if (a.localized) S[3] = "done"
  if (a.fixes.length) S[4] = a.fixOk ? "done" : "fail"
  if (a.fixOk) S[5] = a.review === "reject" ? "fail" : "done"
  const v = verdictOf(s, a, liveElsewhere)
  S[6] = v.tone === "ok" ? "done" : v.tone === "bad" ? "fail" : "halt"
  return S
}

export function plainErr(e: unknown): string {
  const x = String(e || "")
  if (/11434|ollama/i.test(x)) return "The agent could not reach the AI model on this computer (Ollama, port 11434). Check that Ollama is running and the model is installed, then run again."
  if (/docker/i.test(x)) return "There was a Docker problem. Check that Docker Desktop is running, then run again."
  if (/not found/i.test(x)) return "A file or folder the run needed was not found."
  return "Something went wrong inside the agent. The technical message is below."
}

export type Verdict = { tone: Tone; head: string; sum: string }
export function verdictOf(s: JobState, a: Analysis, liveElsewhere: boolean): Verdict {
  const r = s.result,
    sp = s.spec || {}
  if (!r)
    return liveElsewhere
      ? { tone: "", head: "Still running in another window", sum: "This run belongs to another copy of the runner, which is still writing to it. Progress updates here every few seconds." }
      : { tone: "", head: "This run did not finish", sum: "No result was saved. The run was stopped, or the runner was restarted before it finished." }
  if (r.error === "stopped by user" || s.status === "stopped") return { tone: "", head: "Stopped", sum: "The run was stopped before it finished." }
  if (r.error) return { tone: "bad", head: "The run failed", sum: plainErr(r.error) }
  const n = r.attempts ?? a.tests.length
  if (r.verdict === "reproduced") {
    if (r.fix_status === "fixed" && r.resolved === false)
      return { tone: "warn", head: "Bug reproduced; fix not confirmed", sum: "The fix passes the agent's own checks but fails the benchmark's hidden tests." }
    if (r.fix_status === "fixed")
      return {
        tone: "ok",
        head: "Bug reproduced and fixed",
        sum: "The agent wrote a test that shows the bug, then a fix that makes the test pass without breaking the existing tests." + (r.resolved ? " The benchmark's hidden tests confirm it." : ""),
      }
    if (r.fix_status === "not_fixed") return { tone: "warn", head: "Bug reproduced, not fixed", sum: "The agent proved the bug with a failing test, but none of its fixes passed the checks." }
    return { tone: "ok", head: "Bug reproduced", sum: "The agent wrote a test that shows the bug." + (sp.fix === false ? " No fix was requested." : "") }
  }
  if (r.verdict === "needs_more_information") return { tone: "warn", head: "The report needs more detail", sum: "Nothing in the code matched the report well enough to write a meaningful test." }
  return {
    tone: "bad",
    head: "Could not reproduce the bug",
    sum: `After ${n} attempt${n === 1 ? "" : "s"}, no test failed the way the report describes` + (a.stopRepro ? ". It stopped early because the same failure kept repeating." : "."),
  }
}

export function checkRows(s: JobState, a: Analysis): [Tone, string, string][] {
  const r = s.result || {},
    sp = s.spec || {},
    out: [Tone, string, string][] = [],
    n = r.attempts ?? a.tests.length
  out.push(
    r.verdict === "reproduced"
      ? ["ok", "Showed the bug with a failing test", `Yes. It took ${n} attempt${n === 1 ? "" : "s"}.`]
      : ["bad", "Showed the bug with a failing test", `No, not after ${n} attempt${n === 1 ? "" : "s"}.`],
  )
  if (r.verdict === "reproduced" && r.fail_to_pass != null)
    out.push(
      r.fail_to_pass
        ? ["ok", "The test is genuine", "It fails on the buggy code and passes on the developers' fixed version. (Grading only: the agent never saw the fix.)"]
        : ["warn", "The test is genuine", "Not confirmed: the test also fails on the developers' fixed version, so it may not target this exact bug."],
    )
  if (r.fix_status === "fixed")
    out.push(["ok", "Wrote a fix that passes the checks", "The bug's test now passes and the existing tests still pass" + (a.review === "approve" ? "; an independent reviewer approved the change." : ".")])
  else if (r.fix_status === "not_fixed")
    out.push(["bad", "Wrote a fix that passes the checks", a.fixes.length ? `No. None of the ${a.fixes.length} fix attempt${a.fixes.length === 1 ? "" : "s"} passed.` : "No fix passed."])
  else out.push(["skip", "Wrote a fix", sp.fix === false ? "Not requested for this run." : "Skipped: a fix needs a failing test first."])
  if (r.resolved != null)
    out.push(r.resolved ? ["ok", "Benchmark's hidden tests pass", "Independent confirmation that the fix solves the real issue."] : ["bad", "Benchmark's hidden tests pass", "No. The fix did not pass the benchmark's hidden tests."])
  return out
}

/* ---------- log line → plain-language event ---------- */
export type Ev = {
  t: string
  stage: number
  tone: Tone
  title: string
  body: ReactNode
  /** technical extras shown only with Technical details on */
  chips?: { title?: string; items: string[] }
  techLines: string[]
  time?: string
  again?: boolean
}
const C = ({ children }: { children: ReactNode }) => <code className="inline-code">{children}</code>
const joinCodes = (xs: string[]) =>
  xs.map((x, i) => (
    <span key={i}>
      {i ? ", " : ""}
      <C>{x}</C>
    </span>
  ))

/* Each log line becomes a plain-language event; the raw line stays available under Technical details. */
export function parseEv(l: Line, sp: Spec): Ev | null | "raw" {
  const s = l.s,
    t = tagOf(s)
  let m: RegExpMatchArray | null
  const raw = rel(s)
  const B = +(sp.budget ?? 0) || 4
  const ev = (tone: Tone, title: string, body: ReactNode, extra?: { chips?: Ev["chips"]; line?: string }): Ev => ({
    t: t || "",
    stage: t != null && t in TAGSTAGE ? TAGSTAGE[t] : -1,
    tone,
    title,
    body,
    chips: extra?.chips,
    techLines: extra?.line ? [extra.line, raw] : [raw],
  })
  if (t === "runner") {
    m = s.match(/^\[runner\] local project (.*) \(config (\w+)\)/)
    if (m)
      return ev(
        "info",
        "Started",
        <>
          Working on the project in <C>{rel(m[1])}</C>.
        </>,
      )
    m = s.match(/SWE-bench (\S+)/)
    return ev(
      "info",
      "Started",
      m ? (
        <>
          Working on <C>{m[1]}</C> inside a Docker container. The first run of a project downloads it, which can take a few minutes.
        </>
      ) : (
        rel(s.replace(/^\[runner\] /, ""))
      ),
    )
  }
  if (t === "spec") {
    m = s.match(/expected=(['"])(.*)\1 symbols=\[(.*?)\] kind=(\S+)/)
    if (!m) return ev("info", "Read the bug report", s)
    const sy = m[3]
      .split(",")
      .map((x) => x.trim().replace(/^'|'$/g, ""))
      .filter(Boolean)
    return ev(
      "ok",
      "Read the bug report",
      <>
        The bug is {KIND[m[4]] || "unexpected behaviour"}. It should: <i>“{m[2]}”</i>
      </>,
      { chips: { items: [...sy, `kind=${m[4]}`] } },
    )
  }
  if (t === "explore") {
    const it = s
      .slice(10)
      .split(", ")
      .map((x) => x.match(/^(.*)\.([^.(]+)\(([-\d.]+)\)$/))
      .filter((x): x is RegExpMatchArray => !!x)
    if (!it.length) return ev("warn", "Searched the code", "Nothing in the code matched the report closely.")
    return ev(
      "ok",
      "Found the code the report is about",
      <>
        Best match: <C>{it[0][2]}</C> in {it[0][1]}
        {it.length > 1 ? (
          <>
            ; also {joinCodes(it.slice(1, 4).map((x) => x[2]))}.
          </>
        ) : (
          "."
        )}
      </>,
      { chips: { title: "Relevance score: higher means a closer match", items: it.map((x) => `${x[1]}.${x[2]} · ${x[3]}`) } },
    )
  }
  if (t === "relax") return ev("info", "Removed a check the agent made up", "The test checked for an error message the report never mentions, so that check was dropped.")
  if (t === "attempt") {
    m = s.match(/\[attempt (\d+)\] exec=(\S+) exc=(\S+) -> (\S+)/)
    if (!m) return ev("info", "Test attempt", s)
    const c = TEST_CAT[m[4]] || [m[4], "warn", ""]
    return ev(c[1], `Attempt ${m[1]} of up to ${B}: ${c[0]}`, c[2], { line: `ran: ${m[2]} · raised: ${m[3]} · category: ${m[4]}` })
  }
  if (t === "f2p") {
    m = s.match(/passes-on-fixed=(\S+) valid-by-meaning=(\S+)/)
    const ok = !!m && m[1] === "True",
      mean = !!m && m[2] === "True"
    return ev(
      ok || mean ? "ok" : "bad",
      "Graded against the developers' real fix",
      <>
        {ok
          ? "The test fails on the buggy code and passes on the fixed code, so it targets the real bug."
          : mean
            ? "On the fixed code the test only differs in message wording, so it is judged valid by meaning."
            : "The test also fails on the fixed code, so it may not target this exact bug."}{" "}
        <span className="text-muted-foreground">(Grading only: the agent never sees this.)</span>
      </>,
    )
  }
  if (t === "localize" || t === "re-localize") {
    let body = s.replace(/^\[[^\]]+\] /, ""),
      lead = ""
    if ((m = body.match(/^(\d+) (?:failing repro test function\(s\)|still failing); (.*)$/))) {
      lead = `${m[1]} part${m[1] === "1" ? "" : "s"} of the bug's test ${t === "localize" ? "fail" : "still fail"}. `
      body = m[2]
    }
    const it = body
      .split(", ")
      .map((x) => x.match(/^(.*):(.+)\(([-\d.]+)\)$/))
      .filter((x): x is RegExpMatchArray => !!x)
    const head = t === "localize" ? "Ranked the likely faulty functions" : "Looked again after a partial fix"
    if (!it.length) return ev("info", head, lead + rel(body))
    return ev(
      "ok",
      head,
      <>
        {lead}Most likely: <C>{it[0][2]}</C> in {it[0][1]}
        {it.length > 1 ? <>, then {joinCodes(it.slice(1, 3).map((x) => x[2]))}.</> : "."}
      </>,
      { chips: { title: "Suspicion score: higher means more likely", items: it.map((x) => `${x[1]}:${x[2]} · ${x[3]}`) } },
    )
  }
  if (t === "regression") {
    const f = (s.match(/'([^']+)'/g) || []).map((x) => x.slice(1, -1))
    return ev(
      "info",
      "Chose existing tests as a safety net",
      f.length ? <>{joinCodes(f)} will be re-run on every fix, so the fix cannot quietly break something else.</> : "No existing tests matched, so only the bug's own test will check the fix.",
    )
  }
  if (t === "patch") {
    m = s.match(/\[patch (\d+)\] -> (\S+)(?: \((.*)\))?/)
    if (!m) return ev("info", "Fix attempt", s)
    const c = FIX_CAT[m[2]] || [m[2], "warn", ""]
    return ev(
      c[1],
      `Fix attempt ${m[1]} of up to 3: ${c[0]}`,
      <>
        {c[2]}
        {m[3] ? <> Changed: {joinCodes(m[3].split(", ").map(rel))}.</> : null}
      </>,
      { line: `category: ${m[2]}` },
    )
  }
  if (t === "sweep") {
    const b = s.replace(/^\[sweep\] /, "")
    if (/^no similar/.test(b)) return ev("info", "Looked for the same mistake elsewhere", "No similar code was found.")
    if (/^similar code/.test(b)) return ev("info", "Looked for the same mistake elsewhere", `Similar code to check: ${rel(b.replace(/^similar code: /, ""))}`)
    if (/same mistake not present/.test(b)) return ev("ok", "Looked for the same mistake elsewhere", "The similar code does not have the same mistake.")
    m = b.match(/-> (\S+)/)
    const c = m && FIX_CAT[m[1]]
    return ev(c ? c[1] : "info", "Fixed the same mistake in similar code", c ? c[2] : rel(b))
  }
  if (t === "review") {
    const ok = /APPROVE/.test(s)
    const why = s.split(": ").slice(1).join(": ")
    return ev(ok ? "ok" : "bad", ok ? "Reviewer approved the fix" : "Reviewer rejected the fix", ok ? "An independent check of the change found no problems." : `Reason: ${why || "not given"}. The agent will try again.`)
  }
  if (t === "control") {
    const b = s.replace(/^\[control\] /, "")
    if (/stopping/.test(b)) {
      m = b.match(/(\d+) times/)
      return ev("warn", "Stopped repeating itself", `The same failure happened ${m ? m[1] + " times" : "several times"} in a row, so the agent stopped this step instead of wasting time.`)
    }
    m = b.match(/focus on (.*)$/)
    return ev(
      "info",
      "Changed approach",
      m ? (
        <>
          Now focusing on <C>{rel(m[1])}</C>.
        </>
      ) : (
        "Trying a different approach."
      ),
    )
  }
  if (t === "blackboard") return ev("bad", "Safety rule blocked an unchecked fix", "The fix changed after it was tested, so it is not reported as fixed.")
  if (t === "result") return null
  return "raw"
}

/* Turn the log into timeline events, with the same post-processing as the original page. */
export function buildEvents(lines: Line[], s: JobState): Ev[] {
  const sp = s.spec || {}
  const evs: Ev[] = []
  for (const l of lines) {
    const e = parseEv(l, sp)
    if (!e || e === "raw") continue
    if (e.stage < 0) e.stage = evs.length ? evs[evs.length - 1].stage : 0
    e.time = l.t && s.started ? fmt(l.t - s.started) : ""
    evs.push(e)
  }
  /* the worker logs the review before the fix it reviewed */
  for (let i = 0; i + 1 < evs.length; i++)
    if (evs[i].t === "review" && evs[i + 1].t === "patch") {
      const x = evs[i]
      evs[i] = evs[i + 1]
      evs[i + 1] = x
      i++
    }
  evs.forEach((e, i) => {
    if ((e.t === "attempt" || e.t === "patch") && e.tone !== "ok" && evs.slice(i + 1).some((x) => x.t === e.t)) e.again = true
  })
  return evs
}

/* ---------- diffs ---------- */
export type DiffFile = { name: string; lines: string[] }
export function parseDiff(d: unknown): DiffFile[] {
  const files: DiffFile[] = []
  let cur: DiffFile | null = null
  for (const l of String(d || "").split("\n")) {
    if (l.startsWith("diff --git")) {
      cur = { name: l.replace(/^diff --git a\/(\S+) b\/.*$/, "$1"), lines: [] }
      files.push(cur)
      continue
    }
    if (!cur) {
      cur = { name: "", lines: [] }
      files.push(cur)
    }
    if (l.startsWith("+++ ")) {
      if (!cur.name) cur.name = l.slice(4).replace(/^b\//, "")
      continue
    }
    if (/^(--- |index )/.test(l)) continue
    cur.lines.push(l)
  }
  while (files.length && files[files.length - 1].lines.length && files[files.length - 1].lines.at(-1) === "") files[files.length - 1].lines.pop()
  return files
}
