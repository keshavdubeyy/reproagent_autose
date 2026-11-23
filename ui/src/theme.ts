import { useCallback, useEffect, useState } from "react"
import { store } from "@/lib/runner"

export type ThemeMode = "auto" | "light" | "dark"
const asMode = (v: string | null): ThemeMode | null => (v === "auto" || v === "light" || v === "dark" ? v : null)

/* Auto / Light / Dark, saved per viewer under "ra-theme". ?theme=light|dark overrides it for one visit (not saved). */
export function useTheme() {
  const [mode, setMode] = useState<ThemeMode>(() => asMode(new URLSearchParams(location.search).get("theme")) || asMode(store.get("ra-theme")) || "auto")

  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)")
    const apply = () => {
      const el = document.documentElement
      el.classList.toggle("dark", mode === "dark" || (mode === "auto" && mq.matches))
      if (mode === "auto") delete el.dataset.theme
      else el.dataset.theme = mode
    }
    apply()
    mq.addEventListener("change", apply)
    return () => mq.removeEventListener("change", apply)
  }, [mode])

  const cycle = useCallback(() => {
    setMode((cur) => {
      const nx: ThemeMode = cur === "auto" ? "light" : cur === "light" ? "dark" : "auto"
      store.set("ra-theme", nx)
      return nx
    })
  }, [])

  return { mode, label: mode === "light" ? "Light" : mode === "dark" ? "Dark" : "Auto", cycle }
}
