// Shape of `swarm status --json` (schema 1) as far as the panel reads it.
// Fields added after the panel's first version are optional, so an older `swarm` still renders.
export type AskItem = {
  type: 'ask'
  id: number
  project: string | null
  plan: string | null
  question: string
  options?: string | null
  destructive: boolean
  action?: string | null
  created_at?: string
}
export type BlockedItem = {
  type: 'blocked'
  id: number
  plan: string | null
  title: string
  blocked_from: string | null
}
export type RunContext = { tokens: number; window: number }
export type RunItem = {
  type: 'run'
  id: number
  kind: string
  plan: string | null
  minutes: number | null
  purpose?: string | null
  timeout_s?: number | null
  context?: RunContext | null
}
export type PlanItem = {
  type: 'plan'
  id: number
  plan: string | null
  title: string
  state: string
}
export type GoalItem = { type: 'goal'; id: number; project: string; title: string }

// Items of `done`: plans moved to MERGED/DONE and OK runs of the last 24 h.
export type DonePlanItem = {
  type: 'plan'
  id: number
  plan: string | null
  title: string
  state: string
  at?: string
}
export type DoneRunItem = {
  type: 'run'
  id: number
  kind: string
  plan: string | null
  summary?: string | null
  ended_at?: string | null
}

export type ProjectItem = {
  name: string
  branch?: string
  auto: boolean
  counts: Record<string, number>
}

export type LockItem = { name: string; holder?: string | null; since?: string | null }
export type WorktreesInfo = {
  count: number
  cleanup: number
  targets_gb: number
  scanned_at?: string
}

export type StatusData = {
  schema?: number
  waiting: Array<AskItem | BlockedItem>
  running: Array<PlanItem | RunItem>
  next: Array<GoalItem | PlanItem>
  done?: Array<DonePlanItem | DoneRunItem>
  projects?: ProjectItem[]
  header: {
    disk_free_gb: number | null
    disk_floor_gb?: number
    disk_label?: string
    month_spend_usd: number
    monthly_budget_usd: number
    budget_warn?: boolean
    locks?: LockItem[]
    daemon: { on: boolean; age_s: number | null }
    worktrees?: WorktreesInfo | null
    context_warn?: { tokens: number; step: number; alert?: number }
  }
}

// The PM session's context and its running subagents, read from the engine.
export type PmContext = { tokens: number; window: number }
export type SubagentContext = { label: string; tokens: number; window: number | null }

// What the pane draws from: the last good snapshot, when it was taken, the
// first line of the last poll's error (null after a good poll), and the
// engine-side context readings (pm null when they could not be read) and the
// git branch of the session's folder (null outside a repo).
export type PanelView = {
  data: StatusData | null
  takenAt: number | null
  error: string | null
  pm: PmContext | null
  subs: SubagentContext[]
  branch: string | null
}

declare module 'claude-code' {
  interface PluginState {
    'swarm-panel': { view: Shaped<PanelView> }
  }
}
