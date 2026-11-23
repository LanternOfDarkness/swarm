import { expect, mock, test } from 'claude-code/testing'

const PANE = {
  plugin: 'swarm-panel',
  component: 'Pane',
  requestId: 'swarm',
  props: {
    title: 'Swarm',
    isFocused: false,
    bodyColumns: 80,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const ask = (id: number, question: string, destructive = false) => ({
  type: 'ask', id, project: 'demo', plan: 'demo/1048', question,
  options: null, destructive, action: null, created_at: '2026-11-18T10:00:00Z',
})

const board = (over: Record<string, unknown> = {}) => ({
  schema: 1,
  waiting: [
    ask(7, 'Restart the fetcher?\nmore text', true),
    { type: 'blocked', id: 3, project: 'demo', plan: 'demo/0900',
      title: 'Cache tuning', blocked_from: 'FIXING', text: '' },
  ],
  running: [
    { type: 'plan', id: 4, project: 'demo', plan: 'demo/1048',
      title: 'Boot check', state: 'VERIFYING', updated_at: '' },
    { type: 'plan', id: 5, project: 'demo', plan: 'demo/1050',
      title: 'Idle review', state: 'REVIEW', updated_at: '' },
    { type: 'run', id: 11, kind: 'claude', plan: 'demo/1048', session: null,
      started_at: '', minutes: 12, purpose: 'verify', timeout_s: 600 },
  ],
  next: [
    { type: 'goal', id: 1, project: 'demo', title: 'Goal one' },
    { type: 'plan', id: 6, project: 'demo', plan: 'demo/1051', title: 'Plan a', state: 'DRAFT' },
    { type: 'plan', id: 8, project: 'demo', plan: 'demo/1052', title: 'Plan b', state: 'APPROVED' },
    { type: 'plan', id: 9, project: 'demo', plan: 'demo/1053', title: 'Plan c hidden', state: 'DRAFT' },
  ],
  done: [],
  header: {
    disk_free_gb: 61.2, disk_floor_gb: 50, month_spend_usd: 3.1,
    monthly_budget_usd: 80, budget_warn: false, locks: [],
    daemon: { on: true, age_s: 12 }, worktrees: null,
  },
  ...over,
})

type Reply = { exitCode: number; stdout: string; stderr: string }

// Stands for the engine beneath the mod: one swarm reply at a time, and a
// record of the status line, notifications and toasts.
type EngineOpts = {
  store?: Record<string, unknown>
  notifySent?: boolean
  // The PM's context reading, or 'fail' for a $.session.usage() that throws.
  usage?: { tokens: number; window: number } | 'fail'
  agents?: unknown[]
  // What `git branch --show-current` answers in the session's folder.
  git?: Reply
  // What `swarm note` answers (default exit 0).
  note?: Reply
  // What `$.model.fork` answers, or 'throw'.
  fork?: unknown
  // Make the `clear` command reject.
  clearFails?: boolean
}

function engine(on: any, opts: EngineOpts = {}) {
  const seen = {
    status: [] as (string | undefined)[], notify: [] as string[], toast: [] as string[],
    argv: [] as string[][], forks: [] as string[], clears: 0,
  }
  let reply: Reply = { exitCode: 0, stdout: JSON.stringify(board()), stderr: '' }
  const clock = mock.clock(on, { now: Date.UTC(2026, 10, 18, 10, 0, 0) })
  mock.store(on, opts.store ?? {})
  on('command.register', (_: unknown, e: any) => ({ value: { command: e.name } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('session.start', (_: unknown, e: any) => ({ cwd: e.cwd }))
  let git: Reply = opts.git ?? { exitCode: 0, stdout: 'main\n', stderr: '' }
  let note: Reply = opts.note ?? { exitCode: 0, stdout: '', stderr: '' }
  on('process.run', (_: unknown, e: any) => {
    seen.argv.push([...e.argv])
    if (e.argv[0] === 'git') return { value: git }
    if (e.argv[0] === 'swarm' && e.argv[1] === 'note') return { value: note }
    return { value: reply }
  })
  let fork: unknown = opts.fork ?? { isAnswered: true, text: 'Working on 0042.\nNext: verify T1.', usage: {} }
  on('model.fork', (_: unknown, e: any) => {
    seen.forks.push(e.prompt)
    if (fork === 'throw') throw new Error('fork broke')
    return { value: fork }
  })
  on('command.run', { command: 'clear' }, () => {
    seen.clears += 1
    if (opts.clearFails) throw new Error('refused')
    return { text: '' }
  })
  let usage = opts.usage ?? { tokens: 50_000, window: 200_000 }
  let agents = opts.agents ?? []
  on('session.usage', () => {
    if (usage === 'fail') throw new Error('no usage')
    return { value: { startedAt: 0, context: usage, rateLimits: [] } }
  })
  on('agent.list', () => ({ value: agents }))
  on('ui.status', (_: unknown, e: any) => { seen.status.push(e.text); return { value: undefined } })
  on('ui.toast', (_: unknown, e: any) => { seen.toast.push(e.text); return { value: undefined } })
  on('ui.notify', (_: unknown, e: any) => {
    seen.notify.push(e.text)
    return {
      value: opts.notifySent === false
        ? { isSent: false, reason: 'no-channel' }
        : { isSent: true, channel: 'terminal' },
    }
  })
  return {
    seen,
    clock,
    set: (r: Partial<Reply>) => { reply = { exitCode: 0, stdout: '', stderr: '', ...r } },
    setBoard: (b: unknown) => { reply = { exitCode: 0, stdout: JSON.stringify(b), stderr: '' } },
    setUsage: (u: EngineOpts['usage']) => { usage = u ?? usage },
    setAgents: (a: unknown[]) => { agents = a },
    setGit: (r: Reply) => { git = r },
    setNote: (r: Reply) => { note = r },
    setFork: (f: unknown) => { fork = f },
  }
}

async function start($: any, eng: ReturnType<typeof engine>) {
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await eng.clock.settle()
}

test('each section renders from a swarm status reply, on terminal and desktop', async ($, on) => {
  const eng = engine(on)
  await start($, eng)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await ui.find({ type: 'Text', text: /^● daemon 12s$/ }))?.text).toBe('● daemon 12s')
    expect((await ui.find({ type: 'Text', text: /\$3\.10\/\$80/ }))?.text).toBe(' · $3.10/$80 ')
    expect((await ui.find({ type: 'Text', text: /^ · disk/ }))?.text).toBe(' · disk 61 GB')
    expect((await ui.find({ type: 'Text', text: /^Waiting on you/ }))?.text).toBe('Waiting on you (2)')
    expect((await ui.find({ type: 'Text', text: /#7/ }))?.text).toBe('! #7 demo/1048 Restart the fetcher? · 0m')
    expect((await ui.find({ type: 'Text', text: /Cache tuning/ }))?.text).toBe('■ demo/0900 Cache tuning · blocked (was FIXING)')
    expect((await ui.find({ type: 'Text', text: /^Running/ }))?.text).toBe('Running (1)')
    expect((await ui.find({ type: 'Text', text: /claude\/verify/ }))?.text).toBe('▶ demo/1048 claude/verify ▓▓▓▓▓▓▓▓▓▓ 12/10m')
    // The VERIFYING plan has a run, so only the REVIEW plan is listed on its own.
    expect((await ui.find({ type: 'Text', text: /Idle review/ }))?.text).toBe('◆ demo/1050 REVIEW Idle review · idle')
    expect(await ui.find({ type: 'Text', text: /Boot check/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /^Next/ }))?.text).toBe('Next (4)')
    expect((await ui.find({ type: 'Text', text: /Goal one/ }))?.text).toBe('goal #1 demo: Goal one')
    expect(await ui.find({ type: 'Text', text: /Plan c hidden/ })).toBeUndefined()
    expect((await ui.find({ type: 'Text', text: /^updated / }))?.text).toMatch(/^updated \d\d:\d\d:\d\d$/)
    await ui.unmount()
  }
})

test('a failed poll shows the error footer and keeps the old data', async ($, on) => {
  const eng = engine(on)
  await start($, eng)

  eng.set({ exitCode: 1, stderr: 'database is locked\nTraceback ...' })
  await eng.clock.advance(15_000)

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /failed/ }))?.text).toBe('swarm status failed: database is locked')
  expect((await ui.find({ type: 'Text', text: /#7/ }))?.text).toBe('! #7 demo/1048 Restart the fetcher? · 0m')
  expect(eng.seen.status.at(-1)).toBe('swarm: error')
  await ui.unmount()
})

test('a new ask notifies once; an already-seen ask does not', async ($, on) => {
  const eng = engine(on, { store: { seenAsks: [7] } })
  await start($, eng)
  expect(eng.seen.notify).toEqual([])

  eng.setBoard(board({ waiting: [ask(7, 'Restart the fetcher?'), ask(9, 'Merge 1048?')] }))
  await eng.clock.advance(15_000)
  await eng.clock.advance(15_000)

  expect(eng.seen.notify).toEqual(['#9 Merge 1048?'])
  expect(eng.seen.toast).toEqual([])
})

test('the first poll of a brand-new store notifies nothing', async ($, on) => {
  const eng = engine(on)
  await start($, eng)
  await eng.clock.advance(15_000)
  expect(eng.seen.notify).toEqual([])
})

test('a notification that is not sent falls back to a toast', async ($, on) => {
  const eng = engine(on, { store: { seenAsks: [] }, notifySent: false })
  await start($, eng)
  expect(eng.seen.notify).toEqual(['#7 Restart the fetcher?'])
  expect(eng.seen.toast).toEqual(['#7 Restart the fetcher?'])
})

test('status line: running, daemon off with waiting count, error', async ($, on) => {
  const eng = engine(on)
  await start($, eng)
  expect(eng.seen.status.at(-1)).toBe('swarm: 1 running · 2 waiting · PM 50k')

  const off = board()
  ;(off as any).header = { ...(off as any).header, daemon: { on: false, age_s: null } }
  eng.setBoard(off)
  await eng.clock.advance(15_000)
  expect(eng.seen.status.at(-1)).toBe('swarm: daemon off · 2 waiting · PM 50k')

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Text', text: /^○ daemon OFF$/ }))?.text).toBe('○ daemon OFF')
  await ui.unmount()

  eng.set({ exitCode: 0, stdout: 'not json' })
  await eng.clock.advance(15_000)
  expect(eng.seen.status.at(-1)).toBe('swarm: error')
})

// A board from a newer `swarm`, with projects and context warnings.
const v2 = (over: Record<string, unknown> = {}) => {
  const { header, ...rest } = over
  const b = board(rest) as any
  return {
    ...b,
    projects: over.projects ?? [
      { name: 'demo', auto: true, counts: { DRAFT: 2, APPROVED: 1, goals_open: 1 } },
    ],
    header: {
      ...b.header,
      context_warn: { tokens: 150_000, step: 20_000, alert: 200_000 },
      ...((header as object | undefined) ?? {}),
    },
  }
}

const find = async (ui: any, text: RegExp) => ui.find({ type: 'Text', text })

test('PM levels: none under 150k, yellow from 150k, red from 200k', async ($, on) => {
  const eng = engine(on, { usage: { tokens: 140_000, window: 1_000_000 } })
  eng.setBoard(v2())
  await start($, eng)

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    const pm = await find(ui, /^PM /)
    expect(pm?.text).toBe('PM 140k/1M ▓░░░░░░░░░')
    expect(pm?.props.color).toBeUndefined()
    expect(await find(ui, /clean-pm/)).toBeUndefined()
    await ui.unmount()
  }
  expect(eng.seen.status.at(-1)).toBe('swarm: 1 running · 2 waiting · PM 140k')

  eng.setUsage({ tokens: 160_000, window: 1_000_000 })
  await eng.clock.advance(15_000)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await find(ui, /^PM /))?.props.color).toBe('warning')
    const advice = await find(ui, /clean-pm/)
    expect(advice?.text).toBe('→ /clean-pm when convenient')
    expect(advice?.props.color).toBe('warning')
    await ui.unmount()
  }
  expect(eng.seen.status.at(-1)).toBe('swarm: 1 running · 2 waiting · PM 160k → /clean-pm')

  eng.setUsage({ tokens: 210_000, window: 1_000_000 })
  await eng.clock.advance(15_000)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await find(ui, /^PM /))?.props.color).toBe('error')
    const advice = await find(ui, /clean-pm/)
    expect(advice?.text).toBe('→ /clean-pm now')
    expect(advice?.props.color).toBe('error')
    await ui.unmount()
  }
  expect(eng.seen.status.at(-1)).toBe('swarm: 1 running · 2 waiting · PM 210k → /clean-pm now')
})

test('without an alert level (older swarm) 210k is only yellow', async ($, on) => {
  const eng = engine(on, { usage: { tokens: 210_000, window: 1_000_000 } })
  eng.setBoard(v2({ header: { context_warn: { tokens: 150_000, step: 20_000 } } }))
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /^PM /))?.props.color).toBe('warning')
  expect((await find(ui, /clean-pm/))?.text).toBe('→ /clean-pm when convenient')
  await ui.unmount()
})

test('a running subagent gets its own context row', async ($, on) => {
  const eng = engine(on, {
    agents: [
      { id: 'a1', kind: 'local_agent', taskStatus: 'running', label: 'Explore the repo\nmore',
        startTime: 0, cwd: '/', tokenCount: 40_000, contextWindowSize: 200_000, tokenSamples: [] },
      { id: 'a2', kind: 'local_agent', taskStatus: 'completed', label: 'Done one',
        startTime: 0, cwd: '/', tokenCount: 9_000, tokenSamples: [] },
    ],
  })
  eng.setBoard(v2())
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const sub = await find(ui, /sub /)
  expect(sub?.text).toBe('  sub Explore the repo 40k/200k')
  expect(sub?.props.color).toBe('subtle')
  expect(await find(ui, /Done one/)).toBeUndefined()
  await ui.unmount()
})

test('runs show a timeout bar and context; 85 % of either is a warning', async ($, on) => {
  const eng = engine(on)
  const run = (id: number, minutes: number, timeout_s: number | null, ctx: unknown) => ({
    type: 'run', id, kind: 'implementer', plan: `demo/10${id}`, session: null,
    started_at: '', minutes, purpose: 'implement', timeout_s, context: ctx,
  })
  eng.setBoard(v2({
    running: [
      run(61, 3, 600, { tokens: 31_000, window: 200_000 }),
      run(62, 2, 600, { tokens: 850_000, window: 1_000_000 }),
      run(63, 51, 3600, null),
      run(64, 7, null, null),
    ],
  }))
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const ok = await find(ui, /1061/)
  expect(ok?.text).toBe('▶ 1061 implementer/implement ▓▓▓░░░░░░░ 3/10m · ctx 31k/200k')
  expect(ok?.props.color).toBeUndefined()
  const ctx = await find(ui, /1062/)
  expect(ctx?.text).toBe('▶ 1062 implementer/implement ▓▓░░░░░░░░ 2/10m · ctx 850k/1M')
  expect(ctx?.props.color).toBe('warning')
  const late = await find(ui, /1063/)
  expect(late?.text).toBe('▶ 1063 implementer/implement ▓▓▓▓▓▓▓▓▓░ 51/60m')
  expect(late?.props.color).toBe('warning')
  expect((await find(ui, /1064/))?.text).toBe('▶ 1064 implementer/implement 7m')
  await ui.unmount()
})

test('ask options get their own line; a destructive ask makes the title an error', async ($, on) => {
  const eng = engine(on)
  eng.setBoard(v2({
    waiting: [{ ...ask(8, 'Start T2?'), options: 'Start T2|Wait', created_at: '2026-11-18T07:00:00Z' }],
  }))
  await start($, eng)
  let ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /#8/))?.text).toBe('? #8 1048 Start T2? · 3h')
  expect((await find(ui, /\| Wait/))?.text).toBe('  Start T2 | Wait')
  expect((await find(ui, /^Waiting on you/))?.props.color).toBeUndefined()
  await ui.unmount()

  eng.setBoard(v2())
  await eng.clock.advance(15_000)
  ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const titleRow = await find(ui, /^Waiting on you/)
  expect(titleRow?.text).toBe('Waiting on you (2)')
  expect(titleRow?.props.color).toBe('error')
  expect((await find(ui, /#7/))?.props.color).toBe('error')
  await ui.unmount()
})

test('one project drops the project prefix; two keep it and get two lines', async ($, on) => {
  const eng = engine(on)
  eng.setBoard(v2())
  await start($, eng)
  let ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /Cache tuning/))?.text).toBe('■ 0900 Cache tuning · blocked (was FIXING)')
  expect((await find(ui, /^demo ·/))?.text).toBe('demo · 1 goals · 1 ready · 2 draft')
  expect((await find(ui, /^Projects/))?.text).toBe('Projects (1)')
  expect((await find(ui, /Goal one/))?.text).toBe('  goal #1 Goal one')
  expect((await find(ui, /Plan a/))?.text).toBe('  1051 DRAFT Plan a')
  expect(await find(ui, /^Next/)).toBeUndefined()
  expect(await find(ui, /auto off/)).toBeUndefined()
  await ui.unmount()

  eng.setBoard(v2({
    projects: [
      { name: 'demo', auto: true, counts: { DRAFT: 2, goals_open: 0 } },
      { name: 'other', auto: false, counts: { MEASURING: 1, goals_open: 0 } },
    ],
  }))
  await eng.clock.advance(15_000)
  ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /Cache tuning/))?.text).toBe('■ demo/0900 Cache tuning · blocked (was FIXING)')
  expect((await find(ui, /^demo/))?.text).toBe('demo · 2 draft')
  expect((await find(ui, /^other/))?.text).toBe('other · 1 measuring')
  expect((await find(ui, /auto off/))?.props.color).toBe('subtle')
  await ui.unmount()
})

test('done shows the 3 newest of all items under its full count', async ($, on) => {
  const eng = engine(on)
  const planDone = (id: number, at: string) => ({
    type: 'plan', id, project: 'demo', plan: `demo/20${id}`, title: `Plan ${id}`,
    state: 'MERGED', at,
  })
  eng.setBoard(v2({
    done: [
      planDone(11, '2026-11-18T09:00:00Z'),
      planDone(12, '2026-11-17T09:00:00Z'),
      { type: 'run', id: 40, kind: 'reviewer', plan: null, session: null,
        summary: 'Review ok\nmore', cost_usd: 0, ended_at: '2026-11-18T09:30:00Z' },
      planDone(13, '2026-11-18T08:00:00Z'),
      planDone(14, '2026-11-18T07:00:00Z'),
    ],
  }))
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /^Done 24 h/))?.text).toBe('Done 24 h (5)')
  const run = await find(ui, /run #40/)
  expect(run?.text).toBe('✓ run #40 reviewer Review ok · 30m')
  expect(run?.props.color).toBe('subtle')
  const plan = await find(ui, /Plan 11/)
  expect(plan?.text).toBe('✓ 2011 Plan 11 → MERGED · 1h')
  expect(plan?.props.color).toBe('success')
  expect((await find(ui, /Plan 13/))?.text).toBe('✓ 2013 Plan 13 → MERGED · 2h')
  expect(await find(ui, /Plan 14/)).toBeUndefined()
  expect(await find(ui, /Plan 12/)).toBeUndefined()
  await ui.unmount()
})

test('held locks and worktree counts render', async ($, on) => {
  const eng = engine(on)
  eng.setBoard(v2({
    header: {
      locks: [{ name: 'build', holder: 'run 4', since: '2026-11-18T09:55:00Z' }],
      worktrees: { count: 4, cleanup: 2, targets_gb: 12.5, scanned_at: '' },
    },
  }))
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const lock = await find(ui, /^lock /)
  expect(lock?.text).toBe('lock build 5m')
  expect(lock?.props.color).toBe('subtle')
  expect((await find(ui, /^worktrees/))?.text).toBe('worktrees 4 · 2 to clean · 12.5 GB')
  await ui.unmount()
})

test('a snapshot from an older swarm still renders, with Next and no advice', async ($, on) => {
  const eng = engine(on, { usage: { tokens: 190_000, window: 200_000 } })
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /^Next/))?.text).toBe('Next (4)')
  expect(await find(ui, /^Projects/)).toBeUndefined()
  expect(await find(ui, /^Done/)).toBeUndefined()
  expect((await find(ui, /^PM /))?.text).toBe('PM 190k/200k ▓▓▓▓▓▓▓▓▓▓')
  expect(await find(ui, /clean-pm/)).toBeUndefined()
  await ui.unmount()
  expect(eng.seen.status.at(-1)).toBe('swarm: 1 running · 2 waiting · PM 190k')
})

test('a failing session usage shows PM context unknown and the swarm still renders', async ($, on) => {
  const eng = engine(on, { usage: 'fail' })
  eng.setBoard(v2())
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /PM context/))?.text).toBe('PM context: unknown')
  expect((await find(ui, /^Waiting on you/))?.text).toBe('Waiting on you (2)')
  expect(await find(ui, /failed/)).toBeUndefined()
  await ui.unmount()
  expect(eng.seen.status.at(-1)).toBe('swarm: 1 running · 2 waiting')
})

test('the header warns on budget and low disk', async ($, on) => {
  const eng = engine(on)
  eng.setBoard(v2({ header: { budget_warn: true, disk_free_gb: 60, month_spend_usd: 70 } }))
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /^▓/))?.props.color).toBe('warning')
  expect((await find(ui, /^ · disk/))?.props.color).toBe('warning')
  expect((await find(ui, /daemon/))?.props.color).toBe('success')
  await ui.unmount()
})

test('the header shows the session folder git branch, detached, or nothing outside a repo', async ($, on) => {
  const eng = engine(on, { git: { exitCode: 0, stdout: 'feature/x\n', stderr: '' } })
  await start($, eng)
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect((await find(ui, /⎇/))?.text).toBe(' · ⎇ feature/x')
    await ui.unmount()
  }

  eng.setGit({ exitCode: 0, stdout: '', stderr: '' })
  await eng.clock.advance(15_000)
  let ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /⎇/))?.text).toBe(' · ⎇ detached')
  await ui.unmount()

  eng.setGit({ exitCode: 128, stdout: '', stderr: 'fatal: not a git repository' })
  await eng.clock.advance(15_000)
  ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await find(ui, /⎇/)).toBeUndefined()
  expect(await find(ui, /failed/)).toBeUndefined()
  await ui.unmount()
})

test('the header names the disk when swarm sends a disk label', async ($, on) => {
  const eng = engine(on)
  eng.setBoard(v2({ header: { disk_label: 'C:' } }))
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /^ · disk/))?.text).toBe(' · disk C: 61 GB')
  await ui.unmount()
})

test('a project line names its integration branch', async ($, on) => {
  const eng = engine(on)
  eng.setBoard(v2({
    projects: [
      { name: 'demo', branch: 'develop', auto: true, counts: { DRAFT: 2, goals_open: 1 } },
      { name: 'other', auto: true, counts: { goals_open: 0 } },
    ],
  }))
  await start($, eng)
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await find(ui, /^demo/))?.text).toBe('demo (develop) · 1 goals · 2 draft')
  expect((await find(ui, /^other/))?.text).toBe('other')
  await ui.unmount()
})

const notes = (eng: ReturnType<typeof engine>) =>
  eng.seen.argv.filter(a => a[0] === 'swarm' && a[1] === 'note')

test('/clean-pm saves the forked handoff, then clears and refreshes', async ($, on) => {
  const eng = engine(on)
  await start($, eng)
  const polls = eng.seen.argv.filter(a => a[1] === 'status').length
  const { text } = await $.command.run({ command: 'clean-pm' })
  expect(text.startsWith('Handoff saved')).toBe(true)
  expect(text).toContain('Working on 0042.')
  expect(eng.seen.forks.length).toBe(1)
  expect(eng.seen.forks[0]).toContain('You have no tools')
  expect(notes(eng)).toEqual([
    ['swarm', 'note', '--kind', 'handoff', '--', 'Working on 0042.\nNext: verify T1.'],
  ])
  await eng.clock.advance(0)
  expect(eng.seen.clears).toBe(1)
  expect(eng.seen.argv.filter(a => a[1] === 'status').length).toBe(polls + 1)
})

test('/clean-pm keeps a handoff that starts with a dash as one argument', async ($, on) => {
  const eng = engine(on, { fork: { isAnswered: true, text: '-x looks like an option', usage: {} } })
  await start($, eng)
  await $.command.run({ command: 'clean-pm' })
  expect(notes(eng)[0]).toEqual(['swarm', 'note', '--kind', 'handoff', '--', '-x looks like an option'])
})

for (const [name, fork] of [
  ['not answered', { isAnswered: false, reason: 'nothing-to-fork', usage: {} }],
  ['a throw', 'throw'],
  ['an empty answer', { isAnswered: true, text: '   ', usage: {} }],
] as const) {
  test(`/clean-pm with ${name} from the fork saves nothing and does not clear`, async ($, on) => {
    const eng = engine(on, { fork })
    await start($, eng)
    const { text } = await $.command.run({ command: 'clean-pm' })
    expect(text).toContain('nothing saved, session not cleared')
    await eng.clock.advance(0)
    expect(notes(eng)).toEqual([])
    expect(eng.seen.clears).toBe(0)
  })
}

test('/clean-pm keeps the handoff text when swarm note fails, and does not clear', async ($, on) => {
  const eng = engine(on, { note: { exitCode: 1, stdout: '', stderr: 'database is locked\nmore' } })
  await start($, eng)
  const { text } = await $.command.run({ command: 'clean-pm' })
  expect(text).toContain('swarm note failed (database is locked)')
  expect(text).toContain('Working on 0042.')
  await eng.clock.advance(0)
  expect(eng.seen.clears).toBe(0)
})

test('/clean-pm tells the operator to clear when the clear is refused', async ($, on) => {
  const eng = engine(on, { clearFails: true })
  await start($, eng)
  await $.command.run({ command: 'clean-pm' })
  await eng.clock.advance(0)
  expect(eng.seen.clears).toBe(1)
  expect(eng.seen.toast).toContain('handoff saved — type /clear')
})
