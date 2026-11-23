import { atom, read, update } from 'claude-code'
import type { CoreEngineInterface, Register } from 'claude-code'

import type { PanelView, PmContext, StatusData, SubagentContext } from '../types'

// Read-only view of the swarm board. The poll reads the board only
// through `swarm status --json` and never changes it. The one exception is `/clean-pm`:
// it asks one fork for a handoff, saves it as one handoff note and runs /clear.

const PANE = 'swarm'
const TITLE = 'Swarm'
const POLL_MS = 15_000
const RUN_TIMEOUT_MS = 10_000
const SEEN_KEY = 'seenAsks'
const SEEN_MAX = 200
const NOTIFY_MAX = 120
const NEXT_SHOWN = 3
const DONE_SHOWN = 3
const WORKTREE_FLOOR_GB = 15
const WARN_SHARE = 0.8
const RUNNING_PLAN_STATES = ['EXECUTING', 'FIXING', 'VERIFYING', 'REVIEW']

const EMPTY: PanelView = {
  data: null, takenAt: null, error: null, pm: null, subs: [], branch: null,
}
const view = atom({ plugin: 'swarm-panel', key: 'view' } as const, EMPTY, {
  shape: 'view-3',
})

type Timer = { cancel: () => void }
let timer: Timer | undefined

const firstLine = (text: unknown): string =>
  String(text ?? '').split('\n').map(s => s.trim()).find(s => s !== '') ?? ''

const money = (n: number): string => `$${Number.isInteger(n) ? n : n.toFixed(2)}`

const clock = (ms: number): string => {
  const d = new Date(ms)
  const two = (n: number) => String(n).padStart(2, '0')
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`
}

// 135000 → "135k", 1200000 → "1.2M", 1000000 → "1M".
export const tokens = (n: number): string => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, '')}M`
  return `${Math.round(n / 1000)}k`
}

// Minutes under an hour, hours under two days, else days; '' when unknown.
export const age = (at: string | null | undefined, now: number | null): string => {
  if (!at || now === null) return ''
  const t = Date.parse(at)
  if (Number.isNaN(t)) return ''
  const minutes = Math.max(0, Math.floor((now - t) / 60_000))
  if (minutes < 60) return `${minutes}m`
  if (minutes < 48 * 60) return `${Math.floor(minutes / 60)}h`
  return `${Math.floor(minutes / (24 * 60))}d`
}

export const bar = (part: number, whole: number, cells = 10): string => {
  const filled = whole > 0 ? Math.min(cells, Math.max(0, Math.round((cells * part) / whole))) : 0
  return '▓'.repeat(filled) + '░'.repeat(cells - filled)
}

const asks = (data: StatusData) => data.waiting.filter(i => i.type === 'ask')
const runs = (data: StatusData) => data.running.filter(i => i.type === 'run')

// Plans in a running state that have no worker run carrying the same label.
const idlePlans = (data: StatusData) => {
  const withRun = new Set(runs(data).map(r => r.plan))
  return data.running.filter(
    i => i.type === 'plan' && RUNNING_PLAN_STATES.includes(i.state) && !withRun.has(i.plan),
  )
}

type Level = 'yellow' | 'red' | null

// The PM reading's level: red from `alert`, yellow from `tokens`, else none.
export const pmLevel = (v: PanelView): Level => {
  const warn = v.data?.header.context_warn
  if (v.pm === null || warn === undefined) return null
  if (warn.alert !== undefined && v.pm.tokens >= warn.alert) return 'red'
  return v.pm.tokens >= warn.tokens ? 'yellow' : null
}

const LEVEL_COLOR = { yellow: 'warning', red: 'error' } as const
const LEVEL_ADVICE = { yellow: '→ /clean-pm when convenient', red: '→ /clean-pm now' } as const
const LEVEL_SUFFIX = { yellow: ' → /clean-pm', red: ' → /clean-pm now' } as const

export const statusLine = (v: PanelView): string => {
  if (v.error !== null || v.data === null) return 'swarm: error'
  const waiting = v.data.waiting.length
  const level = pmLevel(v)
  const pm = v.pm === null ? '' : ` · PM ${tokens(v.pm.tokens)}${level ? LEVEL_SUFFIX[level] : ''}`
  if (!v.data.header.daemon.on) return `swarm: daemon off · ${waiting} waiting${pm}`
  return `swarm: ${runs(v.data).length} running · ${waiting} waiting${pm}`
}

async function notifyNewAsks($: CoreEngineInterface, data: StatusData): Promise<void> {
  const open = asks(data)
  const stored = await $.store.get(SEEN_KEY)
  if (!Array.isArray(stored)) {
    // Brand-new store: everything open now counts as seen, nothing fires.
    await $.store.set(SEEN_KEY, open.map(a => a.id).slice(-SEEN_MAX))
    return
  }
  const seen = new Set(stored)
  const fresh = open.filter(a => !seen.has(a.id))
  if (fresh.length === 0) return
  // Record before notifying, so a failure below never notifies twice.
  await $.store.set(SEEN_KEY, [...stored, ...fresh.map(a => a.id)].slice(-SEEN_MAX))
  for (const ask of fresh) {
    const text = `#${ask.id} ${firstLine(ask.question)}`.slice(0, NOTIFY_MAX)
    let sent = false
    try {
      sent = (await $.ui.notify(text, { title: 'Swarm ask' })).isSent
    } catch {
      sent = false
    }
    if (!sent) $.ui.toast(text)
  }
}

// The PM's own context and its running subagents. A failure leaves pm null
// ("PM context: unknown") and never touches the swarm data.
async function readContext(
  $: CoreEngineInterface,
): Promise<{ pm: PmContext | null; subs: SubagentContext[] }> {
  let pm: PmContext | null = null
  let subs: SubagentContext[] = []
  try {
    const { context } = await $.session.usage()
    if (typeof context.tokens === 'number') pm = { tokens: context.tokens, window: context.window }
  } catch {
    pm = null
  }
  try {
    subs = (await $.agent.list())
      .filter(a => a.taskStatus === 'running')
      .map(a => ({
        label: firstLine(a.label),
        tokens: a.tokenCount,
        window: a.contextWindowSize ?? null,
      }))
  } catch {
    subs = []
  }
  return { pm, subs }
}

// The git branch checked out where this session runs: null outside a repo,
// 'detached' on a detached HEAD.
async function readBranch($: CoreEngineInterface): Promise<string | null> {
  try {
    const res = await $.process.run(['git', 'branch', '--show-current'], {
      timeoutMs: RUN_TIMEOUT_MS,
    })
    if (res.exitCode !== 0) return null
    return firstLine(res.stdout) || 'detached'
  } catch {
    return null
  }
}

async function poll($: CoreEngineInterface): Promise<void> {
  let data: StatusData | undefined
  let error: string | null = null
  try {
    const res = await $.process.run(['swarm', 'status', '--json'], {
      timeoutMs: RUN_TIMEOUT_MS,
    })
    if (res.exitCode !== 0) {
      error = firstLine(res.stderr) || firstLine(res.stdout) || `exit ${res.exitCode}`
    } else {
      const parsed = JSON.parse(res.stdout)
      if (!parsed || !Array.isArray(parsed.waiting) || !parsed.header) {
        error = 'unexpected swarm status output'
      } else {
        data = parsed as StatusData
      }
    }
  } catch (err) {
    error = firstLine((err as Error)?.message ?? err) || 'swarm status failed'
  }
  const ctx = { ...(await readContext($)), branch: await readBranch($) }

  const now = await $.clock.now()
  const current = await update($, view, old =>
    data
      ? { data, takenAt: now, error: null, ...ctx }
      : { ...EMPTY, ...(old ?? {}), error, ...ctx },
  )
  $.ui.status(statusLine(current))
  if (data) await notifyNewAsks($, data)
}

const HANDOFF_PROMPT =
  'You have no tools; answer straight away. Write the handoff note for the next PM ' +
  'session, from this conversation only. At most 12 short lines, plain text, no ' +
  'headings: what is in progress, what you were about to do next, the operator\'s ' +
  'decisions and open questions that are not yet on the swarm board. Name plans and ' +
  'goals with their titles. Reply with the note only.'
const HANDOFF_MAX = 2000

// /clean-pm: fork a handoff from the conversation, save it on the board, then clear.
async function cleanPm($: CoreEngineInterface): Promise<{ text: string }> {
  let note = ''
  let reason = ''
  try {
    const r = await $.model.fork({ prompt: HANDOFF_PROMPT })
    if (r.isAnswered) note = r.text.trim()
    else reason = r.reason
  } catch (err) {
    reason = firstLine((err as Error)?.message ?? err) || 'fork failed'
  }
  if (!note) {
    return {
      text: `clean-pm: could not write the handoff (${reason || 'empty'}); nothing saved, session not cleared.`,
    }
  }
  note = note.slice(0, HANDOFF_MAX)

  let saveError: string | null = null
  try {
    const res = await $.process.run(['swarm', 'note', '--kind', 'handoff', '--', note], {
      timeoutMs: RUN_TIMEOUT_MS,
    })
    if (res.exitCode !== 0) saveError = firstLine(res.stderr) || `exit ${res.exitCode}`
  } catch (err) {
    saveError = firstLine((err as Error)?.message ?? err) || 'swarm note failed'
  }
  if (saveError !== null) {
    return { text: `clean-pm: swarm note failed (${saveError}); session not cleared.\n\n${note}` }
  }

  // Run /clear once this command has returned: the engine refuses it inside the hook.
  $.clock.after(0, () => {
    void (async () => {
      try {
        await $.command.run({ command: 'clear' })
      } catch {
        $.ui.toast('handoff saved — type /clear')
        return
      }
      await poll($)
    })()
  })
  return { text: `Handoff saved to the swarm board; clearing the session.\n\n${note}` }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'swarm',
      description: 'Swarm panel: toggle it, or "/swarm refresh" to poll now',
      argumentHint: '[refresh]',
    })
    await $.command.register({
      name: 'clean-pm',
      description: 'Write a handoff to the swarm board, then /clear (the next session gets it in its brief)',
    })
    timer?.cancel()
    timer = $.clock.every(POLL_MS, () => void poll($))
    void $.ui.open({ id: PANE, title: TITLE })
    void poll($)
    return next(e)
  })

  on('command.run', { command: 'clean-pm' }, async $ => cleanPm($))

  on('command.run', { command: 'swarm' }, async ($, e) => {
    if (e.args.trim() === 'refresh') {
      await poll($)
      return { text: 'Swarm panel refreshed.' }
    }
    const isOpen = (await $.ui.panes()).some(p => p.id === PANE)
    if (isOpen) {
      await $.ui.close({ id: PANE })
      return { text: 'Swarm panel closed.' }
    }
    await $.ui.open({ id: PANE, title: TITLE })
    return { text: 'Swarm panel opened.' }
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const v = await read($, view)
    const line = (text: string, color?: string) => (
      <Text wrap="truncate-end" color={color}>{text}</Text>
    )
    const title = (text: string, color?: string) => (
      <Text bold wrap="truncate-end" color={color}>{text}</Text>
    )

    if (v.data === null) {
      return (
        <Box flexDirection="column">
          {v.error === null
            ? <Text dimColor>Loading swarm status…</Text>
            : line(`swarm status failed: ${v.error}`, 'warning')}
        </Box>
      )
    }

    const d = v.data
    const hdr = d.header
    const now = v.takenAt
    const projects = d.projects
    const label = (plan: string | null | undefined): string => {
      const text = plan ?? ''
      if (projects?.length !== 1) return text
      const prefix = `${projects[0].name}/`
      return text.startsWith(prefix) ? text.slice(prefix.length) : text
    }
    const withAge = (text: string, at: string | null | undefined) => {
      const a = age(at, now)
      return a === '' ? text : `${text} · ${a}`
    }

    // 1. Header.
    const daemon = hdr.daemon.on ? `● daemon ${hdr.daemon.age_s ?? '?'}s` : '○ daemon OFF'
    const spend = ` · ${money(hdr.month_spend_usd)}/${money(hdr.monthly_budget_usd)} `
    const spendBar = bar(hdr.month_spend_usd, hdr.monthly_budget_usd, 5)
    const diskLow =
      hdr.disk_free_gb !== null &&
      hdr.disk_floor_gb !== undefined &&
      hdr.disk_free_gb < hdr.disk_floor_gb + WORKTREE_FLOOR_GB
    const diskName = hdr.disk_label ? `${hdr.disk_label} ` : ''
    const disk = ` · disk ${diskName}${hdr.disk_free_gb === null ? '?' : Math.round(hdr.disk_free_gb)} GB`
    const lockRows = (hdr.locks ?? []).map(l => {
      const a = age(l.since, now)
      return `lock ${l.name}${a === '' ? '' : ` ${a}`}`
    })

    // 2. Context.
    const level = pmLevel(v)
    const pmLine =
      v.pm === null
        ? 'PM context: unknown'
        : `PM ${tokens(v.pm.tokens)}/${tokens(v.pm.window)} ${bar(v.pm.tokens, v.pm.window)}`
    const subRows = v.subs.map(
      s => `  sub ${s.label} ${tokens(s.tokens)}${s.window === null ? '' : `/${tokens(s.window)}`}`,
    )

    // 3. Waiting on you.
    const anyDestructive = asks(d).some(a => a.destructive)
    const waitRows = d.waiting.flatMap(i => {
      if (i.type !== 'ask') {
        return [line(
          `■ ${label(i.plan)} ${i.title} · blocked (was ${i.blocked_from ?? 'unknown'})`,
          'warning',
        )]
      }
      const head = withAge(
        `${i.destructive ? '!' : '?'} #${i.id} ${label(i.plan ?? i.project)} ${firstLine(i.question)}`,
        i.created_at,
      )
      const rows = [line(head, i.destructive ? 'error' : undefined)]
      if (i.options) rows.push(line(`  ${i.options.split('|').join(' | ')}`, 'subtle'))
      return rows
    })

    // 4. Running.
    const runRows = runs(d).map(r => {
      const minutes = r.minutes ?? 0
      const shown = r.minutes ?? '?'
      let text = `▶ ${label(r.plan)} ${r.kind}/${r.purpose ?? '-'} `
      let warn = false
      if (r.timeout_s) {
        const limit = r.timeout_s / 60
        text += `${bar(minutes, limit)} ${shown}/${Math.round(limit)}m`
        warn = minutes >= WARN_SHARE * limit
      } else {
        text += `${shown}m`
      }
      if (r.context) {
        text += ` · ctx ${tokens(r.context.tokens)}/${tokens(r.context.window)}`
        warn = warn || r.context.tokens >= WARN_SHARE * r.context.window
      }
      return line(text, warn ? 'warning' : undefined)
    })
    const idleRows = idlePlans(d).map(p =>
      line(p.type === 'plan' ? `◆ ${label(p.plan)} ${p.state} ${p.title} · idle` : '', 'subtle'),
    )

    // 5. Projects (or the old Next section without `projects`).
    const nextRows = d.next.slice(0, NEXT_SHOWN)
    const projectSection =
      projects === undefined ? (
        <Box flexDirection="column">
          {title(`Next (${d.next.length})`)}
          {nextRows.map(i =>
            line(i.type === 'goal'
              ? `goal #${i.id} ${i.project}: ${i.title}`
              : `${i.plan ?? ''} ${i.state} ${i.title}`),
          )}
        </Box>
      ) : (
        <Box flexDirection="column">
          {title(`Projects (${projects.length})`)}
          {projects.map(p => {
            const c = p.counts ?? {}
            const parts = [
              [c.goals_open, 'goals'],
              [c.APPROVED, 'ready'],
              [c.DRAFT, 'draft'],
              [c.MEASURING, 'measuring'],
            ].filter(([n]) => typeof n === 'number' && n > 0).map(([n, word]) => ` · ${n} ${word}`)
            return (
              <Box flexDirection="row">
                <Text wrap="truncate-end">{`${p.name}${p.branch ? ` (${p.branch})` : ''}${parts.join('')}`}</Text>
                {p.auto ? null : <Text color="subtle">{' · auto off'}</Text>}
              </Box>
            )
          })}
          {nextRows.map(i =>
            line(i.type === 'goal'
              ? `  goal #${i.id} ${i.title}`
              : `  ${label(i.plan)} ${i.state} ${i.title}`),
          )}
        </Box>
      )

    // 6. Done 24 h, newest first.
    const done = d.done ?? []
    const doneAt = (i: (typeof done)[number]) =>
      Date.parse((i.type === 'plan' ? i.at : i.ended_at) ?? '') || 0
    const doneRows = [...done].sort((a, b) => doneAt(b) - doneAt(a)).slice(0, DONE_SHOWN).map(i =>
      i.type === 'plan'
        ? line(withAge(`✓ ${label(i.plan)} ${i.title} → ${i.state}`, i.at), 'success')
        : line(withAge(`✓ run #${i.id} ${i.kind} ${firstLine(i.summary)}`, i.ended_at), 'subtle'),
    )

    // 7. Footer.
    const wt = hdr.worktrees
    return (
      <Box flexDirection="column">
        <Box flexDirection="row">
          <Text color={hdr.daemon.on ? 'success' : 'warning'}>{daemon}</Text>
          {v.branch === null ? null : <Text>{` · ⎇ ${v.branch}`}</Text>}
          <Text>{spend}</Text>
          <Text color={hdr.budget_warn ? 'warning' : undefined}>{spendBar}</Text>
          <Text wrap="truncate-end" color={diskLow ? 'warning' : undefined}>{disk}</Text>
        </Box>
        {lockRows.map(r => line(r, 'subtle'))}
        {title('Context')}
        {line(pmLine, level ? LEVEL_COLOR[level] : undefined)}
        {level ? line(LEVEL_ADVICE[level], LEVEL_COLOR[level]) : null}
        {subRows.map(r => line(r, 'subtle'))}
        {title(`Waiting on you (${d.waiting.length})`, anyDestructive ? 'error' : undefined)}
        {waitRows}
        {title(`Running (${runs(d).length})`)}
        {runRows}
        {idleRows}
        {projectSection}
        {done.length === 0 ? null : title(`Done 24 h (${done.length})`)}
        {doneRows}
        {wt ? line(`worktrees ${wt.count} · ${wt.cleanup} to clean · ${wt.targets_gb} GB`) : null}
        {v.error === null
          ? <Text dimColor>{`updated ${now === null ? '?' : clock(now)}`}</Text>
          : line(`swarm status failed: ${v.error}`, 'warning')}
      </Box>
    )
  })
}
