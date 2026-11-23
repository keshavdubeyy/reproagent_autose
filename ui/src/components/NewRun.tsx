import type { ReactNode, RefObject } from "react"
import { SearchIcon } from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Field, FieldContent, FieldDescription, FieldLabel, FieldTitle } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { InputGroup, InputGroupAddon, InputGroupInput } from "@/components/ui/input-group"
import { Kbd, KbdGroup } from "@/components/ui/kbd"
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { Textarea } from "@/components/ui/textarea"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { Fold } from "@/components/bits"
import { CFG, TARGETS, type Bug, type CfgKey, type Options, type TargetKey } from "@/lib/runner"

export type Form = {
  target: TargetKey
  cfg: CfgKey
  title: string
  body: string
  bugId: string
  bugQ: string
  repo: string
  fixedp: string
  fix: boolean
  grade: boolean
}

export const sweFilter = (opt: Options | null, q: string): Bug[] => {
  const x = q.toLowerCase()
  return (opt?.swebench || []).filter((b) => !x || b.id.toLowerCase().includes(x) || b.title.toLowerCase().includes(x))
}

const inputCls = "h-auto rounded-sm border-border bg-canvas px-3 py-2.5 text-base text-ink-strong md:text-base dark:bg-canvas"
const choiceCls =
  "min-w-0 cursor-pointer rounded-sm border-border bg-canvas hover:border-muted-foreground has-data-checked:border-primary has-data-checked:bg-canvas has-data-checked:shadow-[inset_0_0_0_1px_var(--primary)] dark:has-data-checked:border-primary dark:has-data-checked:bg-canvas *:data-[slot=field]:px-4 *:data-[slot=field]:py-3.5"

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <div className="grid gap-3">
      <div className="flex items-baseline gap-2.5">
        <span className="text-[13px] font-semibold text-peach">{n}</span>
        <h3 className="m-0 text-lg leading-tight font-semibold text-ink-strong">{title}</h3>
      </div>
      {children}
    </div>
  )
}

function Lbl({ children }: { children: ReactNode }) {
  return <span className="lbl">{children}</span>
}

export function NewRun({
  opt,
  form,
  set,
  pickTarget,
  pickBug,
  filterBugs,
  onRun,
  runDisabled,
  formErr,
  bodyRef,
}: {
  opt: Options | null
  form: Form
  set: (p: Partial<Form>) => void
  pickTarget: (t: TargetKey) => void
  pickBug: (id: string) => void
  filterBugs: (q: string) => void
  onRun: () => void
  runDisabled: boolean
  formErr: string
  bodyRef: RefObject<HTMLTextAreaElement | null>
}) {
  const { target } = form
  return (
    <section className="grid min-w-0 gap-[22px] max-sm:gap-[18px]">
      <header>
        <div className="lbl">A small AI model running on this computer (qwen3:8b)</div>
        <h1 className="mt-2 text-[clamp(28px,4.6vw,36px)] leading-[1.15] font-semibold tracking-[-0.025em] text-ink-strong">
          <em className="font-semibold">Describe</em> a bug. Watch the agent prove it, then fix it.
        </h1>
        <p className="mt-1.5 max-w-[640px] text-[17px]">You write a bug report the way a user would. The agent reads it, finds the code, and works step by step. You can follow every step live.</p>
      </header>
      <ol className="m-0 grid list-none grid-cols-3 gap-[18px] p-0 text-[15px] max-[700px]:grid-cols-1 max-[700px]:gap-2.5">
        <li className="grid gap-0.5">
          <b className="text-[13px] font-semibold tracking-[0.02em] text-peach">1 · Prove it</b>It writes a small test that fails because of the bug.
        </li>
        <li className="grid gap-0.5">
          <b className="text-[13px] font-semibold tracking-[0.02em] text-peach">2 · Find it</b>It ranks the functions most likely to hold the mistake.
        </li>
        <li className="grid gap-0.5">
          <b className="text-[13px] font-semibold tracking-[0.02em] text-peach">3 · Fix it</b>It edits the code and checks the fix with the project's tests.
        </li>
      </ol>

      <Card className="gap-[26px] px-6 py-6 max-sm:px-4 max-sm:py-[18px]">
        <Step n={1} title="Which code should it work on?">
          <RadioGroup value={target} onValueChange={(v) => pickTarget(v as TargetKey)} className="grid grid-cols-3 gap-2.5 max-[700px]:grid-cols-1" aria-label="Which code should it work on?">
            {(Object.keys(TARGETS) as TargetKey[]).map((k) => {
              const t = TARGETS[k]
              return (
                <FieldLabel key={k} htmlFor={`t-${k}`} className={choiceCls}>
                  <Field orientation="horizontal">
                    <FieldContent className="gap-1">
                      <FieldTitle className="text-[17px] leading-tight font-semibold text-ink-strong">{t.name}</FieldTitle>
                      <FieldDescription className="text-sm leading-snug text-foreground">{t.d}</FieldDescription>
                      <span className="text-xs text-muted-foreground">{t.t}</span>
                    </FieldContent>
                    <RadioGroupItem value={k} id={`t-${k}`} className="sr-only" />
                  </Field>
                </FieldLabel>
              )
            })}
          </RadioGroup>
          <Pick opt={opt} form={form} set={set} pickBug={pickBug} filterBugs={filterBugs} />
        </Step>

        <Step n={2} title="Describe the bug">
          <label className="grid gap-1.5">
            <Lbl>Title: one line, what is wrong?</Lbl>
            <Input className={inputCls} value={form.title} onChange={(e) => set({ title: e.target.value })} placeholder="e.g. Discount is applied to small orders" />
          </label>
          <label className="grid gap-1.5">
            <Lbl>Report: what you did, what you expected, what happened instead</Lbl>
            <Textarea ref={bodyRef} className={inputCls + " min-h-[170px] resize-y leading-[1.45]"} value={form.body} onChange={(e) => set({ body: e.target.value })} placeholder="Describe the bug like a user would report it…" />
          </label>
        </Step>

        <Step n={3} title="Agent setup">
          <RadioGroup value={form.cfg} onValueChange={(v) => set({ cfg: v as CfgKey })} className="grid grid-cols-2 gap-2.5 max-[700px]:grid-cols-1" aria-label="Agent setup">
            {(Object.keys(CFG) as CfgKey[]).map((k) => {
              const c = CFG[k]
              return (
                <Tooltip key={k}>
                  <TooltipTrigger asChild>
                    <FieldLabel htmlFor={`c-${k}`} className={choiceCls}>
                      <Field orientation="horizontal">
                        <FieldContent className="gap-1">
                          <FieldTitle className="flex w-full items-baseline justify-between gap-2 text-[17px] leading-tight font-semibold text-ink-strong">
                            <span>
                              {c.name} <span className="text-xs font-normal text-muted-foreground">({k})</span>
                            </span>
                            {c.rec && <span className="rounded-full bg-peach-soft px-2 py-0.5 text-[11px] font-medium whitespace-nowrap text-ink-strong">Recommended</span>}
                          </FieldTitle>
                          <FieldDescription className="text-sm leading-snug text-foreground">{c.d}</FieldDescription>
                        </FieldContent>
                        <RadioGroupItem value={k} id={`c-${k}`} className="sr-only" />
                      </Field>
                    </FieldLabel>
                  </TooltipTrigger>
                  <TooltipContent>{c.tech}</TooltipContent>
                </Tooltip>
              )
            })}
          </RadioGroup>
          <Fold label="Advanced options">
            <div className="mt-3 grid gap-3 pl-5">
              <label className="flex cursor-pointer items-start gap-2.5 text-[15px]">
                <Checkbox className="mt-1" checked={form.fix} onCheckedChange={(v) => set({ fix: v === true })} />
                <span>
                  Also try to write a fix <span className="text-sm text-muted-foreground">(turn off to only reproduce the bug)</span>
                </span>
              </label>
              {target === "swebench" && (
                <label className="flex cursor-pointer items-start gap-2.5 text-[15px]">
                  <Checkbox className="mt-1" checked={form.grade} onCheckedChange={(v) => set({ grade: v === true })} />
                  <span>
                    Grade the result against the developers' real fix and the benchmark's hidden tests <span className="text-sm text-muted-foreground">(the agent never sees these; adds a few minutes)</span>
                  </span>
                </label>
              )}
              <ul className="m-0 list-none p-0 font-mono text-xs leading-normal text-muted-foreground">
                <li>Setup letters as used in the project report:</li>
                {Object.values(CFG).map((c) => (
                  <li key={c.tech}>{c.tech}</li>
                ))}
              </ul>
            </div>
          </Fold>
        </Step>

        <div className="grid gap-2">
          <div className="flex flex-wrap items-center justify-between gap-3.5">
            <div className="flex-[1_1_240px] text-sm text-muted-foreground">{TARGETS[target].eta}</div>
            <Button onClick={onRun} disabled={runDisabled}>
              Run agent →
            </Button>
          </div>
          <div className="text-right text-sm text-muted-foreground">
            <KbdGroup>
              <Kbd>⌘</Kbd>/<Kbd>Ctrl</Kbd> + <Kbd>Enter</Kbd>
            </KbdGroup>{" "}
            also runs it
          </div>
          {formErr && (
            <Alert className="rounded-sm border-0 bg-bad-soft px-3.5 py-3 text-[15px] text-ink-strong">
              <AlertDescription className="text-[15px] text-ink-strong">{formErr}</AlertDescription>
            </Alert>
          )}
        </div>
      </Card>
    </section>
  )
}

function Pick({ opt, form, set, pickBug, filterBugs }: { opt: Options | null; form: Form; set: (p: Partial<Form>) => void; pickBug: (id: string) => void; filterBugs: (q: string) => void }) {
  if (!opt) return null
  if (form.target === "shopcart")
    return (
      <label className="grid gap-1.5">
        <Lbl>Starting bug (fills in the report below; edit it freely)</Lbl>
        <NativeSelect className="w-full" value={form.bugId} onChange={(e) => pickBug(e.target.value)}>
          {opt.shopcart.map((b) => (
            <NativeSelectOption key={b.id} value={b.id}>
              {b.title}  ·  {b.id}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </label>
    )
  if (form.target === "swebench") {
    const list = sweFilter(opt, form.bugQ)
    return (
      <div className="grid gap-1.5">
        <Lbl>Bug ({opt.swebench.length} available · ● = project already downloaded, starts faster)</Lbl>
        <InputGroup className="h-auto rounded-sm bg-canvas dark:bg-canvas">
          <InputGroupAddon>
            <SearchIcon />
          </InputGroupAddon>
          <InputGroupInput className="h-10 text-base md:text-base" value={form.bugQ} onChange={(e) => filterBugs(e.target.value)} placeholder="Filter: django, sympy, flask…" aria-label="Filter bugs" />
        </InputGroup>
        <NativeSelect className="w-full" rows={6} value={form.bugId} onChange={(e) => pickBug(e.target.value)} aria-label="Bug">
          {list.map((b) => (
            <NativeSelectOption key={b.id} value={b.id} className="font-mono">
              {b.image_ready ? "● " : "   "}
              {b.id} · {b.title.slice(0, 70)}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </div>
    )
  }
  return (
    <div className="grid grid-cols-2 gap-2.5 max-[700px]:grid-cols-1">
      <label className="grid gap-1.5">
        <Lbl>Project folder (a Python project with tests)</Lbl>
        <Input className={inputCls} value={form.repo} onChange={(e) => set({ repo: e.target.value })} placeholder="/path/to/project" />
      </label>
      <label className="grid gap-1.5">
        <Lbl>Fixed version folder (optional, used only for grading)</Lbl>
        <Input className={inputCls} value={form.fixedp} onChange={(e) => set({ fixedp: e.target.value })} placeholder="/path/to/fixed" />
      </label>
    </div>
  )
}
