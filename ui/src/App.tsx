import { useCallback, useEffect, useRef, useState } from "react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet"
import { TooltipProvider } from "@/components/ui/tooltip"
import { NewRun, sweFilter, type Form } from "@/components/NewRun"
import { RunView, type JobView } from "@/components/RunView"
import { Brand, SideContent } from "@/components/Sidebar"
import { useTheme } from "@/theme"
import { isCfg, isTarget, setRoot, store, type HistoryRow, type JobState, type Line, type Options, type TargetKey } from "@/lib/runner"

const emptyJob: JobView = { state: null, lines: [], liveElsewhere: false, notFound: false }
const initialForm: Form = { target: "shopcart", cfg: "E", title: "", body: "", bugId: "", bugQ: "", repo: "", fixedp: "", fix: true, grade: true }
const hashId = () => decodeURIComponent(location.hash.slice(1))

export default function App() {
  const theme = useTheme()
  const [opt, setOpt] = useState<Options | null>(null)
  const optRef = useRef<Options | null>(null)
  const [runs, setRuns] = useState<HistoryRow[] | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const JOB = useRef<string | null>(null)
  const [job, setJob] = useState<JobView>(emptyJob)
  const [tabSel, setTabSel] = useState(0)
  const [tech, setTechState] = useState(() => store.get("ra-tech") === "1" || new URLSearchParams(location.search).get("tech") === "1")
  const [side, setSide] = useState(false)
  const [form, setForm] = useState<Form>(initialForm)
  const [formErr, setFormErr] = useState("")
  const [submitting, setSubmitting] = useState(false)
  const bodyRef = useRef<HTMLTextAreaElement | null>(null)

  const set = useCallback((p: Partial<Form>) => setForm((f) => ({ ...f, ...p })), [])
  const setTech = (on: boolean) => {
    setTechState(on)
    store.set("ra-tech", on ? "1" : "0")
  }

  /* ---------- the form's bug picker (was renderPick / prefill) ---------- */
  const prefillFrom = (o: Options | null, target: TargetKey, id: string): Partial<Form> => {
    const list = target === "shopcart" ? o?.shopcart : o?.swebench
    const b = list?.find((x) => x.id === id)
    return b ? { bugId: b.id, title: b.title, body: b.body } : { bugId: id }
  }
  const pickTarget = (t: TargetKey) => {
    const o = optRef.current
    if (t === "folder") set({ target: t, title: "", body: "", repo: "", fixedp: "" })
    else {
      const first = (t === "shopcart" ? o?.shopcart : o?.swebench)?.[0]
      set({ target: t, bugQ: "", ...(first ? prefillFrom(o, t, first.id) : { bugId: "" }) })
    }
  }
  const pickBug = (id: string) => set(prefillFrom(optRef.current, form.target, id))
  const filterBugs = (q: string) => {
    const first = sweFilter(optRef.current, q)[0]
    set({ bugQ: q, ...(first ? prefillFrom(optRef.current, "swebench", first.id) : {}) })
  }

  /* ---------- data ---------- */
  const loadHistory = useCallback(async () => {
    try {
      setRuns(await (await fetch("/api/history")).json())
    } catch {
      /* keep the last list */
    }
  }, [])

  const openJob = useCallback(
    (id: string) => {
      JOB.current = id
      setJobId(id)
      setJob(emptyJob)
      setTabSel(0)
      if (hashId() !== id) history.replaceState(null, "", "#" + id)
      window.scrollTo(0, 0) /* open at the top: status first */
      loadHistory()
    },
    [loadHistory],
  )

  const loadOptions = useCallback(async () => {
    let o: Options
    try {
      o = await (await fetch("/api/options")).json()
    } catch {
      return
    }
    setRoot(o.root)
    const first = !optRef.current
    optRef.current = o
    setOpt(o)
    /* Prefill the starting bug once, when the options first arrive (later refreshes leave the form alone). */
    if (first)
      setForm((f) => {
        if (f.bugId || f.target === "folder") return f
        const b = (f.target === "shopcart" ? o.shopcart : o.swebench)[0]
        return b ? { ...f, bugId: b.id, title: b.title, body: b.body } : f
      })
    if (!JOB.current && !location.hash && o.active) openJob(o.active)
  }, [openJob])

  const newRun = useCallback(() => {
    JOB.current = null
    setJobId(null)
    setSide(false)
    if (location.hash) history.replaceState(null, "", location.pathname + location.search)
    loadHistory()
    loadOptions()
    window.scrollTo(0, 0)
  }, [loadHistory, loadOptions])

  /* first load, hash links, back/forward */
  useEffect(() => {
    if (location.hash.length > 1) openJob(hashId()) /* open a linked run at once; options (docker query) can be slow */
    loadOptions()
    loadHistory()
    const onHash = () => {
      const id = hashId()
      if (id && id !== JOB.current) openJob(id)
      else if (!id && JOB.current) newRun()
    }
    window.addEventListener("hashchange", onHash)
    return () => window.removeEventListener("hashchange", onHash)
  }, [openJob, loadOptions, loadHistory, newRun])

  /* poll the open job: every second while running; every 4 s for an unfinished run in case another runner is still writing it */
  useEffect(() => {
    if (!jobId) return
    let lines: Line[] = [],
      polls = 0,
      live = false,
      busy = false,
      dead = false
    let timer: ReturnType<typeof setInterval> | undefined
    const poll = async () => {
      if (busy || dead) return
      busy = true
      let s: JobState
      try {
        s = await (await fetch(`/api/job/${encodeURIComponent(jobId)}?from=${lines.length}`)).json()
      } catch {
        busy = false
        return
      }
      busy = false
      if (dead) return
      if (s.error) {
        clearInterval(timer)
        setJob({ ...emptyJob, notFound: true })
        return
      }
      setRoot(s.root)
      if (s.status === "interrupted" && polls > 0 && s.lines.length) live = true /* log still growing: another runner owns this job */
      polls++
      lines = s.lines.length ? [...lines, ...s.lines] : lines
      setJob({ state: s, lines, liveElsewhere: live, notFound: false })
      if (s.status !== "running") {
        clearInterval(timer)
        if (s.status === "interrupted") timer = setInterval(poll, 4000)
        if (polls === 1 || s.status !== "interrupted") {
          loadHistory()
          loadOptions()
        }
      }
    }
    poll()
    timer = setInterval(poll, 1000)
    return () => {
      dead = true
      clearInterval(timer)
    }
  }, [jobId, loadHistory, loadOptions])

  /* ---------- actions ---------- */
  const runDisabled = !!opt?.active || submitting
  const run = async () => {
    if (runDisabled) return
    setFormErr("")
    const body: Record<string, unknown> = { target: form.target, config: form.cfg, title: form.title, body: form.body, fix: form.fix, grade: form.grade }
    if (form.target !== "folder") body.bug_id = form.bugId || undefined
    else {
      body.repo = form.repo
      body.fixed = form.fixedp
    }
    setSubmitting(true)
    try {
      const r = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      const j = await r.json()
      if (!r.ok) throw new Error(j.error || r.status)
      location.hash = j.id
    } catch (e) {
      setFormErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSubmitting(false)
    }
  }
  const runRef = useRef(run)
  runRef.current = run
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !JOB.current) {
        e.preventDefault()
        runRef.current()
      }
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [])

  const stopJob = () => {
    if (jobId && confirm("Stop this run? The agent will end where it is.")) fetch(`/api/job/${encodeURIComponent(jobId)}/stop`, { method: "POST" })
  }

  const rerun = () => {
    const sp = job.state?.spec
    if (!sp) return
    newRun()
    const target: TargetKey = isTarget(sp.target) ? sp.target : "shopcart"
    setForm((f) => ({
      ...f,
      target,
      bugId: sp.bug_id || "",
      bugQ: target === "swebench" ? sp.bug_id || "" : "",
      repo: target === "folder" ? sp.repo || "" : "",
      fixedp: target === "folder" ? sp.fixed || "" : "",
      title: sp.title || "",
      body: sp.body || "",
      cfg: isCfg(sp.config) ? sp.config : "E",
      fix: sp.fix !== false,
    }))
    setTimeout(() => bodyRef.current?.focus(), 0)
  }

  const pickRun = (id: string) => {
    location.hash = id
    setSide(false)
  }

  const sideProps = { history: runs, jobId, onNew: newRun, onPick: pickRun, themeLabel: theme.label, onTheme: theme.cycle }

  return (
    <TooltipProvider delayDuration={300}>
      <div className="grid min-h-screen grid-cols-[280px_minmax(0,1fr)] max-[860px]:grid-cols-[minmax(0,1fr)]">
        <aside className="sticky top-0 z-30 h-dvh self-start overflow-hidden bg-paper max-[860px]:hidden" aria-label="Runs">
          <SideContent {...sideProps} />
        </aside>
        <Sheet open={side} onOpenChange={setSide}>
          <SheetContent side="left" className="w-[min(300px,86vw)] gap-0 border-0 bg-paper p-0 shadow-[0_4px_80px_var(--shadow)] sm:max-w-none [&>button]:hidden" aria-label="Runs">
            <SheetTitle className="sr-only">Past runs</SheetTitle>
            <SheetDescription className="sr-only">Start a new run or open a past one.</SheetDescription>
            <SideContent {...sideProps} />
          </SheetContent>
        </Sheet>
        <main className="min-w-0 px-8 pt-9 pb-[120px] max-[860px]:pt-0 max-sm:px-4 max-sm:pb-[90px]">
          <div className="sticky top-0 z-10 -mx-8 mb-6 hidden items-center justify-between gap-2 border-b bg-background px-4 py-3 max-[860px]:flex max-sm:-mx-4">
            <Button variant="secondary" size="sm" onClick={() => setSide(true)} aria-label="Show past runs">
              ☰ Runs
            </Button>
            <Brand className="text-[18px]" />
            <Button size="sm" onClick={newRun}>
              + New
            </Button>
          </div>
          <div className="mx-auto grid max-w-[880px] min-w-0 gap-[22px] max-sm:gap-[18px]">
            <Banner opt={opt} jobId={jobId} />
            {jobId ? (
              <RunView id={jobId} job={job} tech={tech} setTech={setTech} tabSel={tabSel} setTabSel={setTabSel} onNew={newRun} onStop={stopJob} onRerun={rerun} />
            ) : (
              <NewRun opt={opt} form={form} set={set} pickTarget={pickTarget} pickBug={pickBug} filterBugs={filterBugs} onRun={run} runDisabled={runDisabled} formErr={formErr} bodyRef={bodyRef} />
            )}
          </div>
        </main>
      </div>
    </TooltipProvider>
  )
}

function Banner({ opt, jobId }: { opt: Options | null; jobId: string | null }) {
  if (!opt || (!opt.night_running && !(opt.active && opt.active !== jobId))) return null
  const note = "rounded-sm border-0 bg-warn-soft px-3.5 py-3"
  return (
    <div className="grid gap-2">
      {opt.night_running && (
        <Alert className={note}>
          <AlertDescription className="text-[15px] text-ink-strong">The overnight benchmark is using the same AI model and Docker right now. A run here still works, but it will be slower.</AlertDescription>
        </Alert>
      )}
      {opt.active && opt.active !== jobId && (
        <Alert className={note}>
          <AlertDescription className="text-[15px] text-ink-strong">
            <span>
              A run is in progress. Only one runs at a time. <a href={`#${opt.active}`}>Open it</a>
            </span>
          </AlertDescription>
        </Alert>
      )}
    </div>
  )
}
