import { useMemo, type ReactNode } from "react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Bubble, BubbleContent } from "@/components/ui/bubble"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { CodeBlock, CodeBlockContent } from "@/components/ui/code-block"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { Empty, EmptyDescription } from "@/components/ui/empty"
import { Item, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "@/components/ui/item"
import { Marker, MarkerContent, MarkerIcon } from "@/components/ui/marker"
import { Message, MessageContent } from "@/components/ui/message"
import { Skeleton } from "@/components/ui/skeleton"
import { Spinner } from "@/components/ui/spinner"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { Trace, TraceCollapsible, TraceCollapsibleContent, TraceCollapsibleTrigger, TraceDescription, TraceLine, TraceTitle } from "@/components/ui/trace"
import { DiffView, Fold, PyCode, TechLine, ToneBadge, toneSoft, toneText, Tracker } from "@/components/bits"
import { cn } from "@/lib/utils"
import {
  analyse,
  buildEvents,
  cfgName,
  checkRows,
  FIX_CAT,
  fmt,
  isTarget,
  KIND,
  nowState,
  rel,
  STAGES,
  TARGETS,
  TEST_CAT,
  TICK,
  trackStates,
  verdictOf,
  whenOf,
  type Ev,
  type JobState,
  type Line,
} from "@/lib/runner"

export type JobView = { state: JobState | null; lines: Line[]; liveElsewhere: boolean; notFound: boolean }

export function RunView({
  id,
  job,
  tech,
  setTech,
  tabSel,
  setTabSel,
  onNew,
  onStop,
  onRerun,
}: {
  id: string
  job: JobView
  tech: boolean
  setTech: (on: boolean) => void
  tabSel: number
  setTabSel: (i: number) => void
  onNew: () => void
  onStop: () => void
  onRerun: () => void
}) {
  return (
    <section className="grid min-w-0 gap-[22px] max-sm:gap-[18px]">
      <div className="flex flex-wrap items-center justify-between gap-2.5">
        <Button variant="secondary" size="sm" onClick={onNew}>
          ← New run
        </Button>
        <Tooltip>
          <TooltipTrigger asChild>
            <label className="inline-flex cursor-pointer items-center gap-2 text-[13px] font-medium text-muted-foreground">
              <Switch checked={tech} onCheckedChange={setTech} /> Technical details
            </label>
          </TooltipTrigger>
          <TooltipContent>Show raw categories, scores, file paths and log lines</TooltipContent>
        </Tooltip>
      </div>
      {job.notFound ? (
        <Alert className="rounded-sm border-0 bg-bad-soft px-3.5 py-3">
          <AlertDescription className="text-[15px] text-ink-strong">No run called “{id}” was found.</AlertDescription>
        </Alert>
      ) : !job.state ? (
        <Card className="items-center gap-3 px-6 py-6 text-muted-foreground">
          <div className="flex items-center gap-2">
            <Spinner /> Loading…
          </div>
          <Skeleton className="h-4 w-2/3 bg-action" />
          <Skeleton className="h-4 w-1/2 bg-action" />
        </Card>
      ) : (
        <Loaded job={job as JobView & { state: JobState }} tech={tech} tabSel={tabSel} setTabSel={setTabSel} onStop={onStop} onRerun={onRerun} />
      )}
    </section>
  )
}

function Loaded({ job, tech, tabSel, setTabSel, onStop, onRerun }: { job: JobView & { state: JobState }; tech: boolean; tabSel: number; setTabSel: (i: number) => void; onStop: () => void; onRerun: () => void }) {
  const s = job.state,
    sp = s.spec || {},
    lines = job.lines
  const running = s.status === "running"
  const a = useMemo(() => analyse(lines), [lines])
  const live = running || (!s.result && job.liveElsewhere)
  const T = isTarget(sp.target) ? TARGETS[sp.target] : { name: sp.target || "Project", eta: "", max: 0 }
  const evs = useMemo(() => buildEvents(lines, s), [lines, s])

  return (
    <>
      <div className="grid gap-1.5">
        <div className="lbl">
          {T.name}
          {sp.bug_id ? ` · ${sp.bug_id}` : ""} · {cfgName(sp.config)} · {whenOf(s.id)}
        </div>
        <h1 className="m-0 text-[clamp(28px,4.6vw,36px)] leading-[1.15] font-semibold tracking-[-0.025em] text-ink-strong">{sp.title || "Untitled run"}</h1>
      </div>

      {live ? <StatusCard s={s} a={a} running={running} T={T} onStop={onStop} /> : <VerdictCard s={s} a={a} liveElsewhere={job.liveElsewhere} tech={tech} onRerun={onRerun} />}

      <Fold label="The bug report given to the agent">
        <Message align="end" className="mt-2.5">
          <MessageContent>
            <Bubble variant="secondary" align="end" className="max-w-[min(100%,680px)]">
              <BubbleContent className="rounded-lg rounded-br-sm bg-action px-4 py-3 text-[15px] leading-relaxed whitespace-pre-wrap text-ink-strong">{sp.body}</BubbleContent>
            </Bubble>
          </MessageContent>
        </Message>
      </Fold>

      <Timeline evs={evs} live={live} tech={tech} />
      <BehindTheScenes s={s} lines={lines} tech={tech} tabSel={tabSel} setTabSel={setTabSel} />
    </>
  )
}

function StatusCard({ s, a, running, T, onStop }: { s: JobState; a: ReturnType<typeof analyse>; running: boolean; T: { name: string; eta: string; max: number }; onStop: () => void }) {
  const sp = s.spec || {}
  const ns = nowState(a, sp)
  const el = running && s.started ? s.now - s.started : null
  const slow = el != null && T.max && el > T.max
  return (
    <Card aria-live="polite" className="gap-[18px] bg-[linear-gradient(135deg,var(--peach-soft)_0%,var(--paper)_62%)] px-6 py-6 max-sm:px-4 max-sm:py-[18px]">
      <div className="lbl">
        <span className="pulse" />
        {running ? "Running" : "Running elsewhere"} · step {ns.st + 1} of 7
      </div>
      <p className="m-0 text-[clamp(21px,3.2vw,26px)] leading-tight font-semibold tracking-[-0.015em] text-ink-strong">
        {ns.head}
        {ns.tail && (
          <>
            : <em className="font-normal text-foreground">{ns.tail}</em>
          </>
        )}
      </p>
      <p className="m-0 text-[17px]">{ns.sub}</p>
      <Tracker S={trackStates(a, s, true, false, ns)} />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="lbl">
          {ns.next && (
            <>
              Next: {ns.next}
              <br />
            </>
          )}
          {el != null ? `Running for ${fmt(el)}. ` : ""}
          {slow ? "Taking longer than usual; the AI model may be busy." : T.eta || ""}
        </div>
        {running && (
          <Button size="sm" className="bg-bad-soft text-bad hover:bg-bad-soft/80" onClick={onStop}>
            Stop run
          </Button>
        )}
      </div>
    </Card>
  )
}

function VerdictCard({ s, a, liveElsewhere, tech, onRerun }: { s: JobState; a: ReturnType<typeof analyse>; liveElsewhere: boolean; tech: boolean; onRerun: () => void }) {
  const v = verdictOf(s, a, liveElsewhere),
    r = s.result || {},
    d = s.detail
  return (
    <Card className="gap-[18px] px-6 py-6 max-sm:px-4 max-sm:py-[18px]">
      <div className="lbl">
        {r.wall_s != null ? `Finished · took ${fmt(r.wall_s)}` : s.status === "interrupted" ? "Not finished" : "Finished"}
        {r.llm_calls != null ? ` · ${r.llm_calls} AI model call${r.llm_calls === 1 ? "" : "s"}` : ""}
      </div>
      <h2 className={cn("m-0 text-[clamp(25px,3.8vw,32px)] leading-[1.15] font-semibold tracking-[-0.02em]", toneText[v.tone] ?? toneText[""])}>{v.head}</h2>
      <p className="m-0 text-[17px]">{v.sum}</p>
      {s.result && !r.error && (
        <ItemGroup className="gap-2.5" role="list">
          {checkRows(s, a).map(([c, b, x], i) => (
            <Item key={i} role="listitem" className="flex-nowrap items-start gap-3 border-0 p-0">
              <ItemMedia className="self-start">
                <span className={cn("grid size-7 place-items-center rounded-full text-sm font-semibold", toneSoft[c])}>{TICK[c]}</span>
              </ItemMedia>
              <ItemContent className="min-w-0 gap-0.5">
                <ItemTitle className="line-clamp-none w-auto text-base font-semibold text-ink-strong">{b}</ItemTitle>
                <ItemDescription className="line-clamp-none text-[15px] text-foreground">{x}</ItemDescription>
              </ItemContent>
            </Item>
          ))}
        </ItemGroup>
      )}
      {r.error && <TechLine className="mt-0">{rel(r.error)}</TechLine>}
      <Tracker S={trackStates(a, s, false, liveElsewhere)} />
      {d && !d.missing && d.fix?.final_diff && (
        <div className="grid gap-2">
          <h3 className="m-0 text-lg font-semibold text-ink-strong">The fix</h3>
          <div className="text-sm text-muted-foreground">Green lines were added, red lines removed.</div>
          <DiffView diff={d.fix.final_diff} />
        </div>
      )}
      {d && !d.missing && d.final_test && (
        <Fold label="The test that shows the bug">
          <PyCode code={d.final_test} className="mt-2" />
        </Fold>
      )}
      {tech && <TechLine className="mt-0">{rel(JSON.stringify(r))}</TechLine>}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span />
        <Button variant="secondary" size="sm" onClick={onRerun}>
          Edit and run again
        </Button>
      </div>
    </Card>
  )
}

/* The timeline: one Trace group per step, opened by default; each log line is a plain-language event. */
function Timeline({ evs, live, tech }: { evs: Ev[]; live: boolean; tech: boolean }) {
  const groups: { stage: number; evs: Ev[] }[] = []
  for (const e of evs) {
    const g = groups[groups.length - 1]
    if (!g || g.stage !== e.stage) groups.push({ stage: e.stage, evs: [e] })
    else g.evs.push(e)
  }
  return (
    <div className="grid gap-3.5">
      <h2 className="m-0 text-2xl leading-tight font-semibold tracking-[-0.02em] text-ink-strong">What the agent did</h2>
      {evs.length ? (
        <Trace className="gap-1">
          {groups.map((g, gi) => (
            <TraceCollapsible key={`${gi}-${g.stage}`} defaultOpen>
              <TraceCollapsibleTrigger className={cn("py-2 pl-11 max-sm:pl-0", gi === 0 && "pt-0")}>
                <TraceLine>
                  <TraceTitle className="text-[13px] font-medium tracking-[0.02em]">
                    Step {g.stage + 1} · {STAGES[g.stage][0]}
                  </TraceTitle>
                  <TraceDescription className="text-xs">{g.evs.length}</TraceDescription>
                </TraceLine>
              </TraceCollapsibleTrigger>
              <TraceCollapsibleContent className="mt-1 mb-0 ml-0 border-l-0 pr-0 pl-0 text-foreground">
                <ol className="m-0 grid list-none p-0">
                  {g.evs.map((e, i) => (
                    <EventRow key={i} e={e} tech={tech} />
                  ))}
                </ol>
              </TraceCollapsibleContent>
            </TraceCollapsible>
          ))}
        </Trace>
      ) : (
        <Empty className="items-start p-0 text-left">
          <EmptyDescription className="text-sm">Nothing yet.</EmptyDescription>
        </Empty>
      )}
      {live && (
        <Marker className="pl-11 text-sm max-sm:pl-0">
          <MarkerIcon>
            <Spinner className="text-peach" />
          </MarkerIcon>
          <MarkerContent>working…</MarkerContent>
        </Marker>
      )}
    </div>
  )
}

function EventRow({ e, tech }: { e: Ev; tech: boolean }) {
  return (
    <li className="relative grid grid-cols-[30px_minmax(0,1fr)] gap-3.5 pb-3 before:absolute before:top-[30px] before:bottom-0 before:left-[14.5px] before:w-px before:bg-border">
      <span className={cn("grid size-[30px] place-items-center rounded-full text-[13px] font-semibold", toneSoft[e.tone] ?? toneSoft.info)} aria-hidden="true">
        {TICK[e.tone] || "•"}
      </span>
      <div className="min-w-0 rounded-sm bg-card px-4 py-3">
        <div className="flex items-baseline justify-between gap-2.5">
          <span className="font-semibold text-ink-strong">{e.title}</span>
          <span className="font-mono text-xs whitespace-nowrap text-muted-foreground">{e.time ? "+" + e.time : ""}</span>
        </div>
        <div className="mt-1 text-[15px] wrap-anywhere">
          {e.body}
          {e.again ? " The agent tried again." : null}
        </div>
        {tech && e.chips && (
          <div className="mt-1.5 flex flex-wrap gap-1.5" title={e.chips.title}>
            {e.chips.items.map((x, i) => (
              <span key={i} className="rounded-[3px] bg-canvas px-1.5 py-0.5 font-mono text-xs wrap-anywhere">
                {x}
              </span>
            ))}
          </div>
        )}
        {tech &&
          e.techLines.map((x, i) => (
            <TechLine key={i} className={i ? "mt-1" : undefined}>
              {x}
            </TechLine>
          ))}
      </div>
    </li>
  )
}

/* One folded attempt in "Behind the scenes" (was <details class="att">) */
function Att({ head, children }: { head: ReactNode; children: ReactNode }) {
  return (
    <Collapsible className="mb-2 rounded-sm bg-canvas px-3.5 py-2.5">
      <CollapsibleTrigger className="flex w-full cursor-pointer flex-wrap items-center gap-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring">{head}</CollapsibleTrigger>
      <CollapsibleContent className="pt-2">{children}</CollapsibleContent>
    </Collapsible>
  )
}

function BehindTheScenes({ s, lines, tech, tabSel, setTabSel }: { s: JobState; lines: Line[]; tech: boolean; tabSel: number; setTabSel: (i: number) => void }) {
  const d = s.detail
  const tabs: [string, ReactNode][] = []
  if (d && !d.missing) {
    if (d.attempts?.length)
      tabs.push([
        "Test attempts",
        d.attempts.map((x) => {
          const c = TEST_CAT[x.category || ""] || [x.category || "?", "", ""]
          return (
            <Att
              key={x.n}
              head={
                <>
                  <ToneBadge tone={c[1]}>{c[0]}</ToneBadge> <b className="font-semibold text-ink-strong">Test {x.n}</b>
                  {tech && (
                    <span className="font-mono text-xs text-muted-foreground">
                      {x.status} · {x.exception || "no exception"} · {x.category}
                    </span>
                  )}
                </>
              }
            >
              {x.feedback && (
                <p className="mt-0 mb-2">
                  <span className="lbl">Note the agent gave itself:</span> {rel(x.feedback)}
                </p>
              )}
              {x.failure_line && (
                <p className="mt-0 mb-2">
                  <span className="lbl">Failure:</span> <code className="inline-code">{rel(x.failure_line)}</code>
                </p>
              )}
              <PyCode code={x.code || ""} />
            </Att>
          )
        }),
      ])
    if (d.fix?.patches?.length)
      tabs.push([
        "Fix attempts",
        d.fix.patches.map((p) => {
          const c = FIX_CAT[p.category || ""] || [p.category || "?", "", ""]
          return (
            <Att
              key={p.n}
              head={
                <>
                  <ToneBadge tone={c[1]}>{c[0]}</ToneBadge> <b className="font-semibold text-ink-strong">Fix {p.n}</b>
                  {tech && (
                    <span className="font-mono text-xs text-muted-foreground">
                      {p.category}
                      {p.kind ? " · " + p.kind : ""}
                    </span>
                  )}
                </>
              }
            >
              {p.feedback && <p className="mt-0 mb-2">{rel(p.feedback)}</p>}
              {p.diff ? <DiffView diff={p.diff} /> : <div className="text-sm text-muted-foreground">No change produced.</div>}
            </Att>
          )
        }),
      ])
    if (d.spec)
      tabs.push([
        "How it read the report",
        <dl className="m-0 grid grid-cols-[150px_minmax(0,1fr)] gap-x-3.5 gap-y-1.5 text-[15px] max-sm:grid-cols-[minmax(0,1fr)] max-sm:[&_dd]:mb-2">
          <dt className="lbl leading-relaxed">Summary</dt>
          <dd className="m-0">{d.spec.summary}</dd>
          <dt className="lbl leading-relaxed">Expected</dt>
          <dd className="m-0">{d.spec.expected}</dd>
          <dt className="lbl leading-relaxed">Observed</dt>
          <dd className="m-0">{d.spec.observed}</dd>
          <dt className="lbl leading-relaxed">Kind of bug</dt>
          <dd className="m-0">{KIND[d.spec.failure_kind || ""] || d.spec.failure_kind}</dd>
          <dt className="lbl leading-relaxed">Key words</dt>
          <dd className="m-0">{(d.spec.symbols || []).join(", ")}</dd>
          {d.spec.example_inputs?.length ? (
            <>
              <dt className="lbl leading-relaxed">Example steps</dt>
              <dd className="m-0">{d.spec.example_inputs.join("; ")}</dd>
            </>
          ) : null}
        </dl>,
      ])
    if (d.fix?.candidates?.length)
      tabs.push([
        "Suspect functions",
        <ol className="m-0 pl-5">
          {d.fix.candidates.map((c, i) => (
            <li key={i} className="mb-1.5 font-mono text-[13px] wrap-anywhere">
              {rel(c)}
            </li>
          ))}
        </ol>,
      ])
  }
  const rawLog = rel(lines.map((l) => l.s).join("\n"))
  tabs.push([
    "Raw log",
    <CodeBlock className="rounded-sm border-0 bg-code dark:bg-code">
      <CodeBlockContent wrap className="max-h-[380px] text-[12.5px] leading-[1.6] text-ink-strong">
        {rawLog || "(empty)"}
      </CodeBlockContent>
    </CodeBlock>,
  ])
  const sel = tabSel >= tabs.length ? 0 : tabSel
  return (
    <Card className="gap-3.5 px-6 py-6 max-sm:px-4 max-sm:py-[18px]">
      <div>
        <h2 className="m-0 text-2xl leading-tight font-semibold tracking-[-0.02em] text-ink-strong">Behind the scenes</h2>
        <div className="text-sm text-muted-foreground">Everything the agent produced, for anyone who wants the detail.</div>
      </div>
      <Tabs value={String(sel)} onValueChange={(v) => setTabSel(+v)} className="min-w-0 gap-3.5">
        <TabsList className="h-auto w-full flex-wrap justify-start gap-1.5 bg-transparent p-0 group-data-horizontal/tabs:h-auto">
          {tabs.map((t, i) => (
            <TabsTrigger
              key={t[0]}
              value={String(i)}
              className="h-auto flex-none cursor-pointer rounded-full border-0 bg-canvas px-3.5 py-2 text-[13px] font-medium text-foreground after:hidden data-active:bg-primary data-active:text-primary-foreground dark:data-active:border-0 dark:data-active:bg-primary dark:data-active:text-primary-foreground"
            >
              {t[0]}
            </TabsTrigger>
          ))}
        </TabsList>
        {tabs.map((t, i) => (
          <TabsContent key={t[0]} value={String(i)} className="min-w-0">
            {t[1]}
          </TabsContent>
        ))}
      </Tabs>
    </Card>
  )
}
