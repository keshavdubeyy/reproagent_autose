import { Button } from "@/components/ui/button"
import { Empty, EmptyDescription } from "@/components/ui/empty"
import { Item, ItemContent, ItemDescription, ItemTitle } from "@/components/ui/item"
import { Separator } from "@/components/ui/separator"
import { ToneBadge } from "@/components/bits"
import { cn } from "@/lib/utils"
import { badgeOf, CFG, fmt, isCfg, whenOf, type HistoryRow } from "@/lib/runner"

export function Brand({ className }: { className?: string }) {
  return (
    <div className={cn("px-2 text-[21px] leading-tight font-semibold tracking-[-0.02em] text-ink-strong", className)}>
      Repro<i className="font-semibold text-peach">Agent</i> Runner
    </div>
  )
}

export function SideContent({
  history,
  jobId,
  onNew,
  onPick,
  themeLabel,
  onTheme,
}: {
  history: HistoryRow[] | null
  jobId: string | null
  onNew: () => void
  onPick: (id: string) => void
  themeLabel: string
  onTheme: () => void
}) {
  return (
    <div className="flex h-full min-h-0 flex-col gap-3.5 px-3.5 pt-[22px] pb-4">
      <Brand />
      <Button className="w-full" onClick={onNew}>
        + New run
      </Button>
      <div className="lbl px-2 pt-1">Past runs</div>
      <div className="grid min-h-0 flex-1 content-start gap-0.5 overflow-auto" role="list">
        {history && history.length === 0 && (
          <Empty className="items-start p-2.5 text-left">
            <EmptyDescription className="text-sm">No runs yet</EmptyDescription>
          </Empty>
        )}
        {(history || []).map((r) => {
          const [c, l] = badgeOf(r)
          return (
            <Item
              key={r.id}
              asChild
              size="sm"
              className={cn("cursor-pointer gap-1.5 rounded-sm border-0 p-2.5 text-left hover:bg-action", jobId === r.id && "bg-canvas hover:bg-canvas")}
              role="listitem"
            >
              <button type="button" title={r.title} onClick={() => onPick(r.id)} aria-current={jobId === r.id ? "true" : undefined}>
                <ItemContent className="min-w-0 gap-1.5">
                  <ItemTitle className="line-clamp-2 block w-auto text-[15px] leading-snug font-medium text-ink-strong">{r.title}</ItemTitle>
                  <ItemDescription className="line-clamp-none flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                    <ToneBadge tone={c}>{l}</ToneBadge>
                    <span>
                      {whenOf(r.id)}
                      {r.wall_s != null ? ` · ${fmt(r.wall_s)}` : ""} · {isCfg(r.config) ? CFG[r.config].name : r.config}
                    </span>
                  </ItemDescription>
                </ItemContent>
              </button>
            </Item>
          )
        })}
      </div>
      <Separator />
      <div className="flex items-center justify-between gap-2 px-1.5">
        <span className="lbl">Theme</span>
        <Button variant="outline" size="sm" className="bg-transparent" onClick={onTheme}>
          {themeLabel}
        </Button>
      </div>
    </div>
  )
}
