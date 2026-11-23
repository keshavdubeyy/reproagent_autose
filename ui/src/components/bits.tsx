import type { ReactNode } from "react"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/badge"
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible"
import { CodeBlock, CodeBlockActions, CodeBlockContent, CodeBlockCopyButton, CodeBlockHeader, CodeBlockLine, CodeBlockTitle } from "@/components/ui/code-block"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useHighlightedCode } from "@/hooks/use-highlighted-code"
import { parseDiff, rel, STAGES, type StepState, type Tone } from "@/lib/runner"

/* Tone colours shared by badges, ticks and timeline dots */
export const toneSoft: Record<string, string> = {
  ok: "bg-ok-soft text-ok",
  bad: "bg-bad-soft text-bad",
  warn: "bg-warn-soft text-warn",
  run: "bg-peach-soft text-ink-strong",
  skip: "bg-canvas text-muted-foreground border border-border",
  info: "bg-paper text-muted-foreground",
  "": "bg-canvas text-muted-foreground",
}
export const toneText: Record<string, string> = { ok: "text-ok", bad: "text-bad", warn: "text-warn", "": "text-ink-strong" }

export function ToneBadge({ tone, children, className }: { tone: Tone | "run"; children: ReactNode; className?: string }) {
  return <Badge className={cn("h-auto rounded-full px-2.5 py-0.5 text-xs leading-snug font-medium", toneSoft[tone] ?? toneSoft[""], className)}>{children}</Badge>
}

/* A folded section with the page's "+ / −" marker (was <details>). Uncontrolled, so a re-render never closes it. */
export function Fold({ label, children, defaultOpen, className }: { label: ReactNode; children: ReactNode; defaultOpen?: boolean; className?: string }) {
  return (
    <Collapsible defaultOpen={defaultOpen} className={className}>
      <CollapsibleTrigger className="group/fold inline-flex cursor-pointer items-center gap-1.5 rounded-sm text-[13px] font-medium tracking-[0.02em] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">
        <span className="inline-block w-3.5 text-center group-aria-expanded/fold:hidden" aria-hidden>
          +
        </span>
        <span className="hidden w-3.5 text-center group-aria-expanded/fold:inline-block" aria-hidden>
          −
        </span>
        {label}
      </CollapsibleTrigger>
      <CollapsibleContent>{children}</CollapsibleContent>
    </Collapsible>
  )
}

/* Hand-drawn loop around the active step (design.md "drawn annotation") */
const Scribble = () => (
  <svg className="pointer-events-none absolute -inset-x-[9px] -inset-y-[7px] h-[calc(100%+14px)] w-[calc(100%+18px)] overflow-visible text-peach" viewBox="0 0 120 40" preserveAspectRatio="none" aria-hidden="true">
    <path
      d="M14 30 C 3 22, 9 8, 38 5 C 66 2, 108 4, 115 17 C 121 30, 92 37, 60 37 C 30 37, 6 33, 8 20 C 10 12, 22 8, 30 7"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      vectorEffect="non-scaling-stroke"
    />
  </svg>
)

const stepCircle: Record<StepState, string> = {
  done: "bg-primary text-background border-primary",
  active: "bg-peach-soft text-ink-strong border-peach",
  fail: "bg-bad-soft text-bad border-bad",
  halt: "bg-canvas text-muted-foreground border-border border-dashed",
  skip: "bg-canvas text-muted-foreground border-border",
  todo: "bg-canvas text-muted-foreground border-border",
}
const stepLabel: Record<StepState, string> = {
  done: "text-foreground",
  active: "text-ink-strong",
  fail: "text-bad",
  halt: "text-muted-foreground",
  skip: "text-muted-foreground opacity-70 line-through decoration-border",
  todo: "text-muted-foreground opacity-70",
}

/* The 7-step tracker. Below 640px only the circles show, with a caption underneath. */
export function Tracker({ S }: { S: StepState[] }) {
  const cur = S.findIndex((x) => x === "active")
  const capI = cur >= 0 ? cur : Math.max(0, S.reduce((k, x, i) => (x !== "skip" ? i : k), 0))
  return (
    <div className="grid gap-3">
      <ol className="relative m-0 grid list-none grid-cols-7 gap-1.5 p-0" aria-label="Progress">
        {STAGES.map((st, i) => {
          const c = S[i]
          const sym = c === "done" ? "✓" : c === "fail" ? "✕" : c === "skip" ? "–" : c === "halt" ? "■" : String(i + 1)
          const lineOn = c === "done" || c === "active" || c === "fail"
          return (
            <Tooltip key={i}>
              <TooltipTrigger asChild>
                <li className="relative grid min-w-0 justify-items-center gap-2 text-center">
                  {i > 0 && <span className={cn("absolute top-[13px] right-[calc(50%+14px)] left-[calc(-50%+14px)] h-px sm:right-[calc(50%+16px)] sm:left-[calc(-50%+16px)]", lineOn ? "bg-muted-foreground" : "bg-border")} aria-hidden />}
                  <span className={cn("relative z-[1] grid size-7 place-items-center rounded-full border text-[13px] font-semibold", stepCircle[c])}>{sym}</span>
                  <span className={cn("relative px-1 py-0.5 text-xs leading-tight font-medium max-[640px]:hidden", stepLabel[c])}>
                    {c === "active" && <Scribble />}
                    {st[0]}
                  </span>
                </li>
              </TooltipTrigger>
              <TooltipContent className="max-w-60">{st[1]}</TooltipContent>
            </Tooltip>
          )
        })}
      </ol>
      <div className="hidden text-center text-[13px] font-medium text-foreground max-[640px]:block">
        Step {capI + 1} of 7 · {STAGES[capI][0]}
      </div>
    </div>
  )
}

/* A readable per-file diff: file name on top, added lines green, removed lines red. */
export function DiffView({ diff }: { diff: unknown }) {
  return (
    <div className="grid gap-2">
      {parseDiff(diff).map((f, k) => (
        <CodeBlock key={k} className="gap-0 rounded-sm border-0 bg-code dark:bg-code">
          <CodeBlockHeader className="bg-action px-3.5 py-2">
            <CodeBlockTitle className="font-mono text-[13px] text-ink-strong">{rel(f.name) || "Change"}</CodeBlockTitle>
          </CodeBlockHeader>
          <CodeBlockContent className="max-h-[420px] px-0 py-2 text-[13px] leading-[1.6]">
            {f.lines.map((l, i) => {
              const c = l.startsWith("@@") ? "text-muted-foreground" : l.startsWith("+") ? "bg-ok-soft text-ok" : l.startsWith("-") ? "bg-bad-soft text-bad" : "text-ink-strong"
              return (
                <CodeBlockLine key={i} className={cn("mx-0 px-3.5", c)}>
                  {l || " "}
                </CodeBlockLine>
              )
            })}
          </CodeBlockContent>
        </CodeBlock>
      ))}
    </div>
  )
}

/* Python source (the reproduction test), highlighted, with a copy button. */
export function PyCode({ code, className }: { code: string; className?: string }) {
  const lines = useHighlightedCode(code || "", "python")
  return (
    <CodeBlock className={cn("rounded-sm border-0 bg-code dark:bg-code", className)}>
      <CodeBlockActions>
        <CodeBlockCopyButton value={code || ""} />
      </CodeBlockActions>
      <CodeBlockContent className="max-h-[420px] text-[13px] leading-[1.6] text-ink-strong">
        {lines.map((l, i) => (
          <CodeBlockLine key={i}>{l}</CodeBlockLine>
        ))}
      </CodeBlockContent>
    </CodeBlock>
  )
}

/* Monospace technical line (raw log line, categories, JSON) */
export function TechLine({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("mt-2 font-mono text-xs leading-normal wrap-anywhere whitespace-pre-wrap text-muted-foreground", className)}>{children}</div>
}
