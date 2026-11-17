/**
 * DeepSeek Harness 插件：SolidWorks 三视图建模
 *
 * 设计约束（都是踩过的坑）：
 * 1. 不 import 任何 dsh 内部包 —— 本地目录安装的 bundle 是软链接，
 *    从工作区解析 @deepseek-ai/* 会 ERR_MODULE_NOT_FOUND。
 * 2. 工具 execute 里任何逃逸的异常都会杀死宿主进程（Electron main 没有
 *    uncaughtException 兜底），所以：子进程回调、清理、解析全部 try/catch，
 *    永不让 Promise 之外的地方抛错。
 * 3. 临时文件删除在 Windows 上可能因占用/杀软瞬时锁返回 EPERM，
 *    必须带重试并且吞掉失败。
 */
import { spawn } from 'node:child_process'
import {
  mkdtempSync,
  readFileSync,
  writeFileSync,
  rmSync,
  existsSync,
  openSync,
  closeSync,
} from 'node:fs'
import { tmpdir, homedir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

/** 包内自带的 Python 内核目录（随包发布，无需外部依赖） */
const PKG_DIR = dirname(fileURLToPath(import.meta.url))
const PY_DIR = join(PKG_DIR, 'py')

const DEFAULTS = {
  bridgeDir: PY_DIR,
  timeoutMs: 600000,
}

/**
 * 找 Python 解释器，优先级：
 *   config.pythonExe > 环境变量 DSH_SW_PYTHON > DSH 自带运行时 > PATH 上的 python
 */
function resolvePython(cfg) {
  if (cfg.pythonExe) return cfg.pythonExe
  if (process.env.DSH_SW_PYTHON) return process.env.DSH_SW_PYTHON
  const dshPython = join(
    homedir(),
    '.dsh',
    'dsh-runtimes',
    'dsh-primary-runtime',
    'dependencies',
    'python',
    'python.exe',
  )
  if (existsSync(dshPython)) return dshPython
  return 'python'
}

function settings(config) {
  const cfg = config && typeof config === 'object' ? config : {}
  return {
    pythonExe: resolvePython(cfg),
    bridgeDir: cfg.bridgeDir || DEFAULTS.bridgeDir,
    timeoutMs: Number(cfg.timeoutMs) || DEFAULTS.timeoutMs,
  }
}

/** 删除临时目录：带重试，任何失败都吞掉——这里抛错会杀死宿主进程。 */
function safeRemove(dir) {
  for (let i = 0; i < 5; i += 1) {
    try {
      rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 60 })
      return
    } catch {
      try {
        // 极少数情况下句柄还在，忙等一小会儿再试；最后一次放弃即可
        const until = Date.now() + 40
        while (Date.now() < until) {}
      } catch {}
    }
  }
}

function safeClose(fd) {
  try {
    closeSync(fd)
  } catch {}
}

/** 跑一个 Python 进程；stdout/stderr 落临时文件，避免受限沙箱下的管道限制。 */
function runPython(opts, argv, signal, cwd, timeoutMs) {
  return new Promise((resolve) => {
    let work = null
    let fdOut = null
    let fdErr = null
    try {
      work = mkdtempSync(join(tmpdir(), 'dsh-sw-'))
      const outFile = join(work, 'out.log')
      const errFile = join(work, 'err.log')
      fdOut = openSync(outFile, 'w')
      fdErr = openSync(errFile, 'w')
    } catch (err) {
      resolve({ code: -1, stdout: '', stderr: `临时文件创建失败: ${err && err.message}` })
      return
    }

    let child
    try {
      child = spawn(opts.pythonExe, argv, {
        cwd: cwd || opts.bridgeDir,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
        stdio: ['ignore', fdOut, fdErr],
        windowsHide: true,
      })
    } catch (err) {
      safeClose(fdOut)
      safeClose(fdErr)
      safeRemove(work)
      resolve({ code: -1, stdout: '', stderr: `启动 Python 失败: ${err && err.message}` })
      return
    }
    // 子进程已复制这些描述符，父进程不应继续持有，否则删除时可能撞锁
    safeClose(fdOut)
    safeClose(fdErr)

    let killed = false
    let settled = false
    const finish = (result) => {
      if (settled) return
      settled = true
      resolve(result)
    }
    const timer = setTimeout(() => {
      killed = true
      try {
        child.kill()
      } catch {}
    }, Number(timeoutMs) || opts.timeoutMs)
    const onAbort = () => {
      killed = true
      try {
        child.kill()
      } catch {}
    }
    try {
      if (signal && signal.addEventListener) signal.addEventListener('abort', onAbort, { once: true })
    } catch {}

    child.on('error', (err) => {
      try {
        clearTimeout(timer)
        finish({ code: -1, stdout: '', stderr: String((err && err.message) || err) })
      } catch {
        finish({ code: -1, stdout: '', stderr: 'spawn error' })
      }
    })

    child.on('close', (code) => {
      let stdout = ''
      let stderr = ''
      try {
        clearTimeout(timer)
        if (signal && signal.removeEventListener) signal.removeEventListener('abort', onAbort)
      } catch {}
      try {
        stdout = readFileSync(join(work, 'out.log'), 'utf8')
      } catch {}
      try {
        stderr = readFileSync(join(work, 'err.log'), 'utf8')
      } catch {}
      try {
        safeRemove(work)
      } catch {}
      finish({
        code,
        stdout,
        stderr: killed ? stderr + '\n[已取消或超时]' : stderr,
      })
    })
  })
}

function runCli(opts, argv, signal) {
  const script = join(opts.bridgeDir, 'swbuild.py')
  if (!existsSync(script)) {
    return Promise.resolve({
      code: -1,
      stdout: '',
      stderr: `找不到 ${script}；检查插件 config.bridgeDir（当前 ${opts.bridgeDir}）`,
    })
  }
  return runPython(opts, [script, ...argv], signal)
}

function tail(text, limit = 1500) {
  if (!text) return ''
  const t = String(text).trim()
  return t.length <= limit ? t : '…' + t.slice(-limit)
}

function format(res, okHint) {
  const lines = []
  let parsed = null
  try {
    parsed = JSON.parse(res.stdout)
  } catch {}

  if (parsed && parsed.ok !== false) {
    lines.push(okHint)
    if (parsed.bbox_mm) lines.push(`包围盒(mm)：${JSON.stringify(parsed.bbox_mm)}`)
    if (parsed.part) lines.push(`零件：${parsed.part}`)
    if (parsed.views) {
      for (const [k, v] of Object.entries(parsed.views)) lines.push(`视图 ${k}：${v}`)
    }
    if (parsed.drawing_png) lines.push(`三视图：${parsed.drawing_png}`)
    if (Array.isArray(parsed.steps)) {
      const bad = parsed.steps.filter((s) => !s.ok)
      lines.push(`步骤 ${parsed.steps.length} 个，失败 ${bad.length} 个`)
      for (const b of bad) lines.push(`  第${b.step}步 ${b.op} 失败：${b.error}`)
    }
  } else if (parsed && parsed.ok === false) {
    lines.push(`失败：${parsed.error || '未知错误'}`)
  } else {
    lines.push(tail(res.stdout, 2500) || `退出码 ${res.code}`)
  }

  const err = tail(res.stderr, 1200)
  if (err) lines.push(`stderr：${err}`)
  return lines.join('\n')
}

/** 手写 registry 需要的工具定义（等价于 defineTool 的编译结果）。 */
function tool(def) {
  const required = Object.keys(def.params || {}).filter((k) => def.params[k].required)
  const properties = {}
  for (const [k, v] of Object.entries(def.params || {})) {
    properties[k] = { type: v.type, ...(v.description ? { description: v.description } : {}) }
  }
  return {
    name: def.name,
    description: def.description,
    parameters: { type: 'object', properties, ...(required.length ? { required } : {}) },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(args, exec) {
      try {
        return await def.run(args || {}, exec || {})
      } catch (err) {
        // 兜底：工具失败只应变成错误结果，绝不能让异常逃到宿主
        return `工具执行失败：${(err && err.message) || err}`
      }
    },
  }
}

export const inject = ['tools']

export function apply(ctx, config) {
  const opts = settings(config)
  const register = (def) => ctx.effect(() => ctx.tools.register(tool(def)))

  register({
    name: 'sw_model',
    description:
      '在 SolidWorks 里按 JSON 规格建模，导出前/上/右/等轴测视图，可同时生成第一角三视图工程图。' +
      '单位毫米；坐标 X 向右、Y 向上、Z 朝观察者。主视图轮廓用 plane="front"，俯视图用 plane="top"，侧视图用 plane="right"；' +
      '实体面上的槽孔用 face="top|bottom|front|back|right|left"，工具会自动建偏移基准面。',
    params: {
      spec_text: {
        type: 'string',
        description:
          '规格 JSON 文本，例如 {"name":"bracket","steps":[{"op":"boss","plane":"front","rect":[-50,-30,50,30],"depth":12}]}。与 spec 二选一。',
      },
      spec: { type: 'string', description: '规格 JSON 文件路径，与 spec_text 二选一' },
      out_dir: { type: 'string', description: '输出目录，默认 out' },
      drawing: { type: 'string', description: '填 yes 则同时生成第一角三视图工程图 PNG' },
    },
    async run(args, exec) {
      let specPath = args.spec
      let tempDir = null
      if (!specPath) {
        if (!args.spec_text) throw new Error('需要 spec_text 或 spec 其中之一')
        tempDir = mkdtempSync(join(tmpdir(), 'dsh-swspec-'))
        specPath = join(tempDir, 'spec.json')
        writeFileSync(specPath, args.spec_text, 'utf8')
      }
      const argv = ['build', specPath, '-o', args.out_dir || 'out']
      if (String(args.drawing || '').toLowerCase() === 'yes') argv.push('--drawing')
      try {
        const res = await runCli(opts, argv, exec.signal)
        return format(res, '建模完成')
      } finally {
        if (tempDir) safeRemove(tempDir)
      }
    },
  })

  register({
    name: 'sw_view',
    description:
      '给已有 SolidWorks 零件导出指定方向的视图 PNG。views 用逗号分隔：front、top、right、left、iso。',
    params: {
      part: { type: 'string', required: true, description: 'SLDPRT 文件绝对路径' },
      views: { type: 'string', description: '逗号分隔的视图名，默认 front,top,right,iso' },
      out_dir: { type: 'string', description: 'PNG 输出目录，默认与零件同目录' },
    },
    async run(args, exec) {
      const argv = ['shot', args.part, '--views', args.views || 'front,top,right,iso']
      if (args.out_dir) argv.push('-o', args.out_dir)
      return format(await runCli(opts, argv, exec.signal), '视图已导出')
    },
  })

  register({
    name: 'sw_drawing',
    description: '由已有零件生成第一角投影三视图（主/俯/左）工程图并导出 PNG，用于和原图纸逐项比对。',
    params: {
      part: { type: 'string', required: true, description: 'SLDPRT 文件绝对路径' },
      out_png: { type: 'string', description: '输出 PNG 路径，默认与零件同目录 _drawing.png' },
    },
    async run(args, exec) {
      const argv = ['drawing', args.part]
      if (args.out_png) argv.push('-o', args.out_png)
      return format(await runCli(opts, argv, exec.signal), '三视图工程图已生成')
    },
  })

  register({
    name: 'sw_doctor',
    description:
      '自检 SolidWorks 链路：Python、pywin32、SW 版本与零件模板。建模报错时先跑它，能区分“环境没通”和“参数写错”。',
    params: {},
    async run(_args, exec) {
      const script = [
        'import json, sys',
        'd = {"python": sys.version.split()[0]}',
        'try:',
        '    import win32com.client as w',
        '    sw = w.Dispatch("SldWorks.Application")',
        '    d["sw"] = sw.RevisionNumber',
        '    d["template"] = sw.GetUserPreferenceStringValue(8)',
        'except Exception as exc:',
        '    d["error"] = str(exc)[:300]',
        'print(json.dumps(d, ensure_ascii=False))',
      ].join('\n')
      const res = await runPython(opts, ['-c', script], exec.signal, tmpdir(), 30000)
      let detail = tail(res.stdout, 800) || '（Python 没有输出）'
      try {
        const parsed = JSON.parse(res.stdout)
        detail = Object.entries(parsed)
          .map(([k, v]) => `${k}: ${v}`)
          .join('\n')
        detail += parsed.error ? '\n结论：链路不通，按 error 排查' : '\n结论：链路可用'
      } catch {}
      const err = tail(res.stderr, 600)
      return ['环境自检', detail, err ? `stderr：${err}` : ''].filter(Boolean).join('\n')
    },
  })
}
