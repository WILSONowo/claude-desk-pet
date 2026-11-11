import type { EngineInterface, Register } from 'claude-code'

// 累计的 token 用量（四类分开记，命中率 = 缓存读取 / 全部输入）
type Totals = {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
}

// 桌宠的状态（悬浮窗程序按这个名字换表情）
// hello：新会话开场打招呼（不算"在等你"）；oops：一次工具调用失败（Claude 一般会自己处理，桌宠只皱一下眉）；
// error：这一轮以出错结束；ended：会话关了，桌宠看到就删掉这个文件
type PetState =
  | 'hello' | 'thinking' | 'tool' | 'reading' | 'waiting' | 'celebrate' | 'error' | 'aborted' | 'stone' | 'oops' | 'ended'

type ProjectTotals = Totals & { costUsd: number; sessions: number }

type Usage = {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
}

// 本会话的记账：totals 是累计用量，lastCost 是上次记账时的会话花费（美元），用来算项目累计的增量
// 存在 $.store 里按会话 id 分开（不用 $.state，那样 plugin.json 就不需要 types 字段，旧版本也能装）
type SessionRecord = { totals: Totals; lastCost: number }
const EMPTY: Totals = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }

// 读资料类工具 → 记笔记表情；其余 → 敲键盘
const READING = new Set(['Read', 'Grep', 'Glob', 'WebFetch', 'WebSearch', 'NotebookRead'])

// /pet 的子命令（只认英文）；参数是否合法由桌宠检查，打错了它会在气泡里说
const PET_COMMANDS = new Set(['', 'show', 'hide', 'home', 'size', 'follow', 'roam', 'sound', 'chat', 'break', 'rest', 'quit'])

const PET_HELP = [
  '/pet 用法：',
  '/pet                      召唤桌宠；被藏起来了就显示出来',
  '/pet hide                 藏起来',
  '/pet home                 回到当前窗口并重新跟随（不坐窗口时回到屏幕右下角）',
  '/pet size small|medium|large   调大小',
  '/pet follow on|off        自动跟着当前窗口',
  '/pet roam on|off          坐在窗口上（关掉就固定在一个位置）',
  '/pet sound on|off         提示音',
  '/pet chat on|off          闲聊',
  '/pet break on|off|30|45|50|60|90   休息提醒开关 / 间隔（分钟）',
  '/pet break test           试一下休息提醒',
  '/pet rest                 我去休息了',
  '/pet quit                 退出桌宠',
].join('\n')

// 会话级的变量（重载 mod 时会在 session.start 里重新算）
let runDir = ''
let petFile = ''
let project = ''
let cwd = ''
let sessionKey = ''
let toolErrors = 0

// 写给桌宠的状态：主循环自己的状态 + 正在干活的小助手（子代理）。两边谁的动静新就说谁的
const AGENT_TOOLS = new Set(['Agent', 'Task'])
const AGENT_TEXT = '派小助手干活中…'
const HELPER_STALE_MS = 15 * 60_000           // 小助手这么久没动静就当它已经结束了（漏掉了结束事件）
let mainBusy = false                          // 主循环这一轮还没结束
let mainStatus: [PetState, string] = ['hello', '我来啦～']
let helperStatus: [PetState, string] = ['tool', '在干活…']
const helpers = new Map<string, number>()     // 小助手 id → 最近一次动静的时间
let seq = 0, mainSeq = 0, helperSeq = 0       // 谁的动静更新（主循环还是小助手）
let turnAt = 0                                // 你最近一次发消息（这一轮开始）的时间；桌宠按它决定跟哪个会话
let since = 0, shownState = ''                // 当前状态从什么时候开始的（换了文字不算换状态）

const fmt = (n: number) =>
  n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : `${n}`

const hitRate = (t: Totals) => {
  const all = t.input + t.cacheRead + t.cacheWrite
  return all === 0 ? '—' : `${Math.round((t.cacheRead / all) * 100)}%`
}

const baseName = (p: string) => p.split(/[\\/]/).filter(Boolean).pop() ?? p

// 气泡里显示的项目名：worktree（<仓库>/.claude/worktrees/<名字>）显示所属仓库的名字
const projectName = (dir: string) => {
  const p = dir.replace(/\\/g, '/').replace(/\/+$/, '')
  const wt = p.match(/^(.*)\/\.claude\/worktrees\/[^/]+$/)
  return baseName(wt ? wt[1] : p)
}

// 现在该给桌宠看什么：主循环在等小助手、或者小助手的动静更新时说小助手在干什么；
// 这一轮结束了但小助手还在后台，就在"轮到你啦"后面带一句
function compose(
  main: [PetState, string], busy: boolean, helper: [PetState, string], count: number, helperNewer: boolean,
): [PetState, string] {
  const [state, text] = main
  if (count === 0) return main
  if (!busy) {
    if (state === 'waiting' || state === 'celebrate') {
      return [state, `轮到你啦！${count === 1 ? '小助手还在后台' : `${count} 个小助手在后台`}`]
    }
    return main
  }
  if (text === AGENT_TEXT || helperNewer) {
    return [helper[0], count === 1 ? `小助手${helper[1]}` : `小助手×${count} ${helper[1]}`]
  }
  return main
}

// 工具名 → 气泡里的一句话
const describe = (tool: string, input: Record<string, unknown>) => {
  const file = typeof input.file_path === 'string' ? baseName(input.file_path) : ''
  if (tool === 'Bash' || tool === 'PowerShell') return '在跑命令…'
  if (tool === 'Edit' || tool === 'Write') return file ? `在改 ${file}` : '在改文件…'
  if (tool === 'Read') return file ? `在读 ${file}` : '在翻文件…'
  if (tool === 'Grep' || tool === 'Glob') return '在找东西…'
  if (tool === 'WebSearch' || tool === 'WebFetch') return '在上网查…'
  if (AGENT_TOOLS.has(tool)) return AGENT_TEXT
  if (tool.startsWith('mcp__')) return describeMcp(tool)
  return `在用 ${tool}…`
}

// MCP 工具名形如 mcp__<服务名>__<工具名>，服务名有时是一串 id，直接显示不好看
const describeMcp = (tool: string) => {
  const [, server = '', name = ''] = tool.split('__')
  if (/browser|chrome|playwright|puppeteer/i.test(server)) return '在操作浏览器…'
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(server) || !server) return name ? `在用 ${name}…` : '在用扩展工具…'
  return `在用 ${server}…`
}

async function readSession($: EngineInterface) {
  return ((await $.store.get(sessionKey)) as SessionRecord | undefined) ?? { totals: EMPTY, lastCost: 0 }
}

// 写状态文件给悬浮窗读；sound 让悬浮窗播放提示音（引擎在 Windows 上没有播放器）
// 排队写：几个小助手同时有动静时不会互相覆盖。出错也不能影响 Claude 干活
let writing: Promise<void> = Promise.resolve()
function show($: EngineInterface, sound?: 'done' | 'error') {
  writing = writing.then(() => writeStatus($, sound)).catch(() => {})
  return writing
}

async function writeStatus($: EngineInterface, sound?: 'done' | 'error') {
  if (!petFile) return
  const ts = await $.clock.now()
  for (const [id, at] of helpers) if (ts - at > HELPER_STALE_MS) helpers.delete(id)
  const [state, text] = compose(mainStatus, mainBusy, helperStatus, helpers.size, helperSeq > mainSeq)
  if (state !== shownState) {
    shownState = state
    since = ts
  }
  await $.fs.write(petFile, JSON.stringify({ state, text, project, ts, since, turn: turnAt, helpers: helpers.size, sound }))
}

// 主循环换状态
function setMain($: EngineInterface, state: PetState, text: string, sound?: 'done' | 'error') {
  mainStatus = [state, text]
  mainSeq = ++seq
  return show($, sound)
}

async function refreshProject($: EngineInterface) {
  try {
    project = projectName(await $.session.root())
  } catch {}
}

async function launchPet($: EngineInterface) {
  try {
    // 引擎不按 PATH 找程序，交给 Windows 自带的 cmd.exe 去找 python
    // （cmd.exe 的路径必须用反斜杠，用正斜杠时 cmd 会把 /Windows 当成开关）
    const ran = await $.process.run(
      ['C:\\Windows\\System32\\cmd.exe','/d', '/c', 'python', `${$.plugin.root}/pet/launch.py`, `${$.plugin.root}/pet/pet.pyw`, runDir],
      { timeoutMs: 15000 },
    )
    if (ran.exitCode !== 0) {
      await $.fs.write(`${runDir}/mod-launch.log`, `exit=${ran.exitCode}\n${ran.stdout}\n${ran.stderr}`)
    }
  } catch (err) {
    $.ui.log(`桌宠启动失败：${String(err)}`, { to: 'debug' })
    await $.fs.write(`${runDir}/mod-launch.log`, `启动 python 失败：${String(err)}`).catch(() => {})
  }
}

// 把 /pet 的参数交给桌宠：写进 command.json，桌宠每 0.25 秒读一次，按 id 去重，30 秒以上的旧命令不执行
async function sendPetCommand($: EngineInterface, args: string) {
  const ts = await $.clock.now()
  const id = `${ts}-${Math.random().toString(36).slice(2, 8)}`
  await $.fs.write(`${runDir}/command.json`, JSON.stringify({ id, args, ts }))
}

// 状态栏的中英文
type Lang = 'zh' | 'en'
const STATUS_TEXT = {
  zh: { session: '当前会话', input: '输入', output: '输出', cacheHit: '缓存命中', context: '上下文', fiveHour: '5h额度' },
  en: { session: 'Session', input: 'Input', output: 'Output', cacheHit: 'Cache hit', context: 'Context', fiveHour: '5h Limit' },
}

// 系统语言是中文就用中文，否则用英文
const systemLang = (): Lang => {
  try {
    return /^zh/i.test(Intl.DateTimeFormat().resolvedOptions().locale) ? 'zh' : 'en'
  } catch {
    return 'zh'
  }
}

// 语言：桌宠右键菜单里选的（存在 settings.json 的 lang），没选过就跟随系统
async function statusLang($: EngineInterface): Promise<Lang> {
  try {
    const text = await $.fs.read(`${runDir}/settings.json`)
    const lang = typeof text === 'string' ? JSON.parse(text).lang : undefined
    if (lang === 'zh' || lang === 'en') return lang
  } catch {}
  return systemLang()
}

async function refreshStatus($: EngineInterface) {
  const t = (await readSession($)).totals
  const usage = await $.session.usage()
  const s = STATUS_TEXT[await statusLang($)]
  const parts = [
    `${s.session} ${s.input} ${fmt(t.input + t.cacheRead + t.cacheWrite)}`,
    `${s.output} ${fmt(t.output)}`,
    `${s.cacheHit} ${hitRate(t)}`,
  ]
  if (usage.context.percent !== undefined) parts.push(`${s.context} ${usage.context.percent}%`)
  const fiveHour = usage.rateLimits.find(r => r.kind === 'five_hour')
  if (fiveHour) parts.push(`${s.fiveHour} ${fiveHour.percentUsed}%`)
  $.ui.status(parts.join(' · '))
}

// 把一次请求的用量记进会话累计和项目累计（项目累计跨会话保存）；u 为空时只更新花费
async function account($: EngineInterface, u: Usage | undefined) {
  const { totals: t, lastCost } = await readSession($)
  const add: Totals = u
    ? { input: u.input_tokens, output: u.output_tokens, cacheRead: u.cache_read_input_tokens, cacheWrite: u.cache_creation_input_tokens }
    : EMPTY
  const usage = await $.session.usage()
  const cost = usage.cost?.usd ?? 0
  const record: SessionRecord = {
    totals: {
      input: t.input + add.input,
      output: t.output + add.output,
      cacheRead: t.cacheRead + add.cacheRead,
      cacheWrite: t.cacheWrite + add.cacheWrite,
    },
    lastCost: cost,
  }
  await $.store.set(sessionKey, record)

  const key = `project:${cwd}`
  const p = ((await $.store.get(key)) as ProjectTotals | undefined) ?? { ...EMPTY, costUsd: 0, sessions: 1 }
  await $.store.set(key, {
    input: p.input + add.input,
    output: p.output + add.output,
    cacheRead: p.cacheRead + add.cacheRead,
    cacheWrite: p.cacheWrite + add.cacheWrite,
    costUsd: p.costUsd + Math.max(0, cost - lastCost),
    sessions: p.sessions,
  })
}

// 每次模型请求结束就记一次账并刷新状态栏。记账排队执行：子代理并行时几个请求可能同时结束，
// 同时读-改-写 $.store 会互相覆盖
let accounting: Promise<void> = Promise.resolve()
function recordUsage($: EngineInterface, u: Usage | undefined) {
  accounting = accounting
    .then(() => account($, u))
    .then(() => refreshStatus($))
    .catch(err => $.ui.log(`用量统计失败：${String(err)}`, { to: 'debug' }))
  return accounting
}

async function statsText($: EngineInterface) {
  const t = (await readSession($)).totals
  const p = (await $.store.get(`project:${cwd}`)) as ProjectTotals | undefined
  const usage = await $.session.usage()
  const line = (name: string, x: Totals) =>
    `${name}：输入 ${fmt(x.input + x.cacheRead + x.cacheWrite)}（缓存读取 ${fmt(x.cacheRead)}、缓存写入 ${fmt(x.cacheWrite)}、未缓存 ${fmt(x.input)}） · 输出 ${fmt(x.output)} · 缓存命中 ${hitRate(x)}`
  const rows = [
    line('本会话', t) + (usage.cost ? ` · $${usage.cost.usd.toFixed(2)}` : ''),
    p ? line(`本项目（${project}，${p.sessions} 个会话）`, p) + ` · $${p.costUsd.toFixed(2)}` : '本项目：还没有记录',
    ...usage.rateLimits.map(r =>
      `额度 ${r.kind === 'five_hour' ? '5 小时' : r.kind === 'seven_day' ? '7 天' : r.kind}：已用 ${r.percentUsed}%` +
      (r.resetsAt ? `，${new Date(r.resetsAt).toLocaleString('zh-CN', { hour12: false })} 重置` : ''),
    ),
    '（项目累计从装上这个 mod 后开始算）',
  ]
  return rows.join('\n')
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    cwd = e.cwd
    project = baseName(e.cwd)
    // 运行目录放在 ~/.claude/desk-pet，不放进 mod 文件夹（避免触发热重载）
    const home = ((await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? '').replace(/\\/g, '/')
    runDir = `${home}/.claude/desk-pet`
    const sessionId = await $.session.id()
    petFile = `${runDir}/sessions/${sessionId}.json`
    sessionKey = `session:${sessionId}`

    // 新会话在项目累计里记一次
    if ((await $.store.get(sessionKey)) === undefined) {
      await $.store.set(sessionKey, { totals: EMPTY, lastCost: 0 })
      const key = `project:${cwd}`
      const p = (await $.store.get(key)) as ProjectTotals | undefined
      if (p) await $.store.set(key, { ...p, sessions: p.sessions + 1 })
    }

    await $.command.register({ name: 'token-stats', description: '查看本会话和本项目累计的 token 用量、缓存命中率' })
    await $.command.register({
      name: 'pet',
      description: '召唤和控制桌宠（/pet help 看用法）',
      argumentHint: '[hide|home|size|follow|roam|sound|chat|break|rest|quit|help]',
      immediate: true,
    })

    await refreshProject($)
    mainBusy = false
    helpers.clear()
    shownState = ''
    turnAt = 0
    await setMain($, 'hello', '我来啦～')
    void launchPet($)
    void refreshStatus($).catch(() => {})
    return started
  })

  on('turn.start', async ($, e, next) => {
    try {
      if (e.agentId === undefined) {
        // 你发了新消息：重新看一下项目名（会话可能刚换了文件夹或 worktree），记下这一轮开始的时间
        toolErrors = 0
        mainBusy = true
        await refreshProject($)
        turnAt = await $.clock.now()
        await setMain($, 'thinking', '思考中…')
      } else {
        // 小助手（子代理）开工
        helpers.set(e.agentId, await $.clock.now())
        await show($)
      }
    } catch {}
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const isMain = e.agentId === undefined
    const input = e as unknown as Record<string, unknown>
    const status: [PetState, string] = [READING.has(e.tool) ? 'reading' : 'tool', describe(e.tool, input)]
    if (isMain) {
      await setMain($, ...status)
    } else if (!AGENT_TOOLS.has(e.tool)) {
      // 小助手的动作：记下来，桌宠说"小助手在读 xx"；顺便证明这个会话还活着
      helpers.set(e.agentId, await $.clock.now())
      helperStatus = status
      helperSeq = ++seq
      await show($)
    }

    const ran = await next(e)

    if (isMain) {
      if (ran.deny === undefined && ran.isError === true) {
        toolErrors += 1
        // 连续出错 3 次：石化彩蛋；偶尔一次失败很常见，Claude 会自己想办法，只皱一下眉
        if (toolErrors >= 3) await setMain($, 'stone', '石化了…')
        else await setMain($, 'oops', '出错了，想想办法…')
      } else {
        toolErrors = 0
        // 派出去的小助手有的还没回来：继续说小助手的事
        if (!(AGENT_TOOLS.has(e.tool) && helpers.size > 0)) await setMain($, 'thinking', '思考中…')
        else await show($)
      }
    }
    return ran
  }).catch(($, e, next) => next(e)) // 桌宠这边出任何错，都照常放行工具调用

  // 一轮里的每次模型请求（主循环和子代理都算）：响应完整收到后立刻记账，状态栏不用等整轮结束
  // 只读结果、不改内容；记账出错只写调试日志，不影响模型回复
  on('turn.step', async function* ($, e, next) {
    const response = yield* next(e)
    await recordUsage($, response.usage ?? undefined)
    return response
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    // 用量已经在 turn.step 里按请求记过了（一轮的用量就是各次请求之和），这里只刷新额度和上下文
    await recordUsage($, undefined)

    if (e.agentId !== undefined) {
      // 小助手收工
      helpers.delete(e.agentId)
      await show($)
      return done
    }

    mainBusy = false
    if (e.reason === 'aborted') {
      await setMain($, 'aborted', '被打断了…')
    } else if (e.reason === 'error' || e.reason === 'refusal') {
      await setMain($, 'error', '出错了，来看看？', 'error')
    } else if (e.durationMs > 180_000) {
      // 干了 3 分钟以上的大活：撒花
      await setMain($, 'celebrate', '搞定啦！轮到你～', 'done')
    } else {
      await setMain($, 'waiting', '轮到你啦！', 'done')
    }
    return done
  })

  // 会话关掉了：告诉桌宠，它会删掉这个会话的状态文件，不再当成"在等你"
  on('session.end', async ($, e, next) => {
    try {
      mainBusy = false
      helpers.clear()
      await setMain($, 'ended', '')
    } catch {}
    return next(e)
  })

  on('command.run', { command: 'token-stats' }, async ($, e, next) => ({ text: await statsText($) }))

  on('command.run', { command: 'pet' }, async ($, e, next) => {
    const args = e.args.trim()
    const head = args.split(/\s+/)[0].toLowerCase()
    if (!PET_COMMANDS.has(head)) return { text: PET_HELP }
    try {
      if (head !== 'quit' && head !== 'hide') await launchPet($) // 已经开着的话不会开第二个；退出/藏起来没必要现拉起一个
      await sendPetCommand($, args)
    } catch (err) {
      $.ui.log(`桌宠命令失败：${String(err)}`, { to: 'debug' })
      return { text: '桌宠没有响应，看看 ~/.claude/desk-pet/mod-launch.log' }
    }
    if (args === '') {
      return { text: '桌宠已召唤（如果它本来就开着，就不会再开第二个）。输入 /pet help 看还能做什么。' }
    }
    return { text: `已告诉桌宠：/pet ${args}` }
  })
}
