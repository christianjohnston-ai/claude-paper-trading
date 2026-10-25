#!/usr/bin/env node
// Re-checks this skill against the code it describes: every repo path, pytest node id, `bin/pa` subcommand,
// step and flag, and navigator flag named in the skill's markdown must still exist.
// Why: a skill that names a flag the CLI no longer has sends a cold session down a dead end; the code is the
// authority (paper-advisor/paper_advisor/cli.py), so this reads it statically and runs nothing in the app.
//
// Usage: node .claude/skills/operating-paper-advisor/scripts/verify_skill_references.mjs [--root <project root>]
// Exit 0 when everything resolves, 1 when anything is missing (each miss is printed with its file), 2 when the
// project root is not found. Skipped on purpose: placeholders (<run_id>), scratch paths under /private/tmp (they
// vanish), SQL text and prose outside code spans.
// SYNC WARNING: candidatePath() encodes the path convention stated at the top of the skill's files.
import { readFile, readdir, stat } from 'fs/promises';
import { dirname, join, resolve } from 'path';
import { fileURLToPath } from 'url';

const here = dirname(fileURLToPath(import.meta.url));
const skillDir = resolve(here, '..');
const argRoot = process.argv.indexOf('--root');
// Default root: this file sits at <root>/.claude/skills/<skill>/scripts/.
const root = argRoot > 0 ? resolve(process.argv[argRoot + 1]) : resolve(here, '../../../..');
const app = join(root, 'paper-advisor');
const pkg = join(app, 'paper_advisor');

const exists = async (p) => stat(p).then(() => true, () => false);

// Basenames of every file in the app and the navigator's scripts, for bare names like `yahoo.py` that the skill
// lists beside a full path. Runtime folders and the venv are skipped: they are not code.
async function basenames(dir, out = new Set()) {
  for (const e of await readdir(dir, { withFileTypes: true }).catch(() => [])) {
    if (['.venv', 'data', 'backups', 'exports', 'reports', '__pycache__', '.pytest_cache'].includes(e.name)) continue;
    if (e.isDirectory()) await basenames(join(dir, e.name), out);
    else out.add(e.name);
  }
  return out;
}
// Folders the app creates at run time (paths.AppPaths.ensure_dirs). A clean checkout has none of them, and their
// contents depend on what has run, so a path inside one is not checked at all.
// SYNC WARNING: RUNTIME_ROOTS mirrors the folders in paper-advisor/paper_advisor/paths.py.

if (!(await exists(join(pkg, 'cli.py')))) {
  console.error(`No paper-advisor/paper_advisor/cli.py under ${root}; pass --root <project root>.`);
  process.exit(2);
}

const cliSrc = await readFile(join(pkg, 'cli.py'), 'utf-8');
const navSrc = await readFile(join(root, '.claude/skills/trading-skills-navigator/scripts/recommend.py'), 'utf-8')
  .catch(() => '');
const subcommands = new Set([...cliSrc.matchAll(/add_parser\("([\w-]+)"/g)].map((m) => m[1]));
const steps = new Set([...cliSrc.matchAll(/choices=\[([^\]]*)\]/g)].flatMap((m) => [...m[1].matchAll(/"([\w-]+)"/g)].map((x) => x[1])));
const cliFlags = new Set([...cliSrc.matchAll(/"(--[\w-]+)"/g)].map((m) => m[1]));
// argparse adds these itself, so they never appear in cli.py's source.
for (const f of ['--help', '-h']) cliFlags.add(f);
const navFlags = new Set([...navSrc.matchAll(/"(--[\w-]+)"/g)].map((m) => m[1]));

// Inline code spans and fenced blocks are where the skill names concrete things; prose is not checked.
function codeFragments(md) {
  const out = [];
  for (const m of md.matchAll(/```[\w]*\n([\s\S]*?)```/g)) out.push(...m[1].split('\n'));
  const noFences = md.replace(/```[\w]*\n[\s\S]*?```/g, '');
  for (const m of noFences.matchAll(/`([^`\n]+)`/g)) out.push(m[1]);
  return out.map((s) => s.replace(/\s#\s.*$/, '').trim()).filter(Boolean);
}

// Placeholders (<run_id>), globs, alternatives and ellipses describe families of paths, not one path.
const isConcrete = (t) => !/[<>*{}|]|\.\.\./.test(t);

function candidatePath(tok) {
  const t = tok.replace(/[),.:;]+$/, '').replace(/\/$/, '');
  if (!isConcrete(t)) return null;
  if (t.startsWith(root + '/')) return t;
  if (/^(\.claude|paper-advisor)\//.test(t)) return join(root, t);
  if (/^(paper_advisor|tests|config|bin|data|reports|backups|exports)\//.test(t)) return join(app, t);
  if (/^(dashboard|data_sources|migrations|skill_runners)\//.test(t)) return join(pkg, t);
  if (/^static\//.test(t)) return join(pkg, 'dashboard', t);
  // A bare module name (`broker_sim.py`) is a file in the package.
  if (/^[a-z_]+\.py$/.test(t)) return join(pkg, t);
  return null;
}

const RUNTIME_ROOTS = ['data', 'reports', 'backups', 'exports'];
// Files Claude Code itself creates under .claude/ on a working machine (a hook cache, the scheduler's lock). The skill
// names them so nobody commits them; a fresh clone has neither, so their absence is not a broken reference.
// SYNC WARNING: the .claude/ entries in .gitignore and the "Never stage" line in testing-and-release.md.
const HARNESS_RUNTIME = ['.claude/tsc-cache', '.claude/scheduled_tasks.lock'];
const codeNames = await basenames(app);
for (const n of await basenames(join(root, '.claude/skills/trading-skills-navigator/scripts'))) codeNames.add(n);

const failures = [];
const counts = { paths: 0, tests: 0, attrs: 0, commands: 0, flags: 0 };

async function checkPathToken(tok, file) {
  const [pathPart, testName] = tok.split('::');
  const p = candidatePath(pathPart);
  if (!p) return;
  counts.paths += 1;
  const rel = p.startsWith(app + '/') ? p.slice(app.length + 1) : '';
  if (RUNTIME_ROOTS.includes(rel.split('/')[0])) return; // runtime state: may legitimately not exist yet
  if (HARNESS_RUNTIME.some((h) => p === join(root, h))) return; // Claude Code's own runtime files, never shipped
  if (/^[a-z_]+\.py$/.test(pathPart.replace(/[),.:;]+$/, ''))) {
    if (!codeNames.has(pathPart.replace(/[),.:;]+$/, ''))) failures.push(`${file}: no file named ${pathPart} in the app`);
    return;
  }
  if (!(await exists(p))) {
    // `data_sources/http.Fetcher` names an attribute of a module: check the module file defines it.
    const attr = p.match(/^(.*)\.([A-Za-z_]\w*)$/);
    const mod = attr && `${attr[1]}.py`;
    if (mod && (await exists(mod))) {
      const src = await readFile(mod, 'utf-8');
      if (new RegExp(`^\\s*(def|class) ${attr[2]}\\b|^${attr[2]}\\s*[:=]`, 'm').test(src)) return;
      return failures.push(`${file}: ${attr[2]} not defined in ${pathPart.replace(/\.[A-Za-z_]\w*$/, '.py')}`);
    }
    return failures.push(`${file}: path not found: ${pathPart}`);
  }
  if (testName) {
    counts.tests += 1;
    const name = testName.replace(/[),.:;`]+$/, '');
    const src = await readFile(p, 'utf-8');
    if (!new RegExp(`def ${name}\\(`).test(src)) failures.push(`${file}: test not found: ${pathPart}::${name}`);
  }
}

// `submission.apply_decisions_tx` names an attribute of a package module: the module must define it. A dotted token
// whose first part is not a package module (`briefing.json`, `Handler._host_ok`) is not a module reference.
const FILE_EXTS = new Set(['py', 'json', 'md', 'sql', 'sqlite', 'txt', 'csv', 'zip', 'gz', 'log', 'mjs', 'js', 'css', 'ini', 'lock', 'yaml', 'xml', 'html']);
async function checkDottedAttr(tok, file) {
  const m = tok.replace(/[),.:;`]+$/, '').match(/^([a-z_]+)\.([A-Za-z_]\w*)$/);
  if (!m || FILE_EXTS.has(m[2])) return;
  const mod = [join(pkg, `${m[1]}.py`), join(pkg, 'dashboard', `${m[1]}.py`), join(pkg, 'data_sources', `${m[1]}.py`)];
  for (const f of mod) {
    if (!(await exists(f))) continue;
    counts.attrs += 1;
    const src = await readFile(f, 'utf-8');
    if (!new RegExp(`^\\s*(def|class) ${m[2]}\\b|^${m[2]}\\s*[:=]`, 'm').test(src)) {
      failures.push(`${file}: ${m[2]} not defined in module ${m[1]}`);
    }
    return;
  }
}

function checkPaCommand(frag, file) {
  // Matches `pa ...`, `bin/pa ...`, `paper-advisor/bin/pa ...` (not pa.py).
  const m = frag.match(/(?:^|[\s(])(?:paper-advisor\/)?(?:bin\/)?pa\s+(.+)$/);
  if (!m) return;
  const toks = m[1].split(/\s+/).filter((t) => isConcrete(t) && !/^["'(]/.test(t));
  let i = 0;
  while (toks[i] && toks[i].startsWith('--')) { // global flags come before the subcommand
    counts.flags += 1;
    if (!cliFlags.has(toks[i])) failures.push(`${file}: unknown global flag ${toks[i]} in: ${frag}`);
    i += 1;
  }
  const sub = toks[i];
  if (!sub || !/^[a-z][\w-]*$/.test(sub)) return;
  counts.commands += 1;
  if (!subcommands.has(sub)) return failures.push(`${file}: unknown subcommand '${sub}' in: ${frag}`);
  const step = toks[i + 1];
  if (step && /^[a-z][\w-]*$/.test(step) && steps.has(step) === false && ['morning', 'review', 'experiment', 'config', 'calendar', 'evidence'].includes(sub)) {
    failures.push(`${file}: unknown step '${step}' for '${sub}' in: ${frag}`);
  }
  for (const t of toks.slice(i + 1)) {
    if (!t.startsWith('--')) continue;
    counts.flags += 1;
    if (!cliFlags.has(t.split('=')[0])) failures.push(`${file}: unknown flag ${t} in: ${frag}`);
  }
}

function checkNavigatorCommand(frag, file) {
  if (!frag.includes('recommend.py') && !/^--(query|no-api|format|time-budget|experience|project-root)\b/.test(frag)) return;
  for (const t of frag.split(/\s+/)) {
    if (!/^--[\w-]+$/.test(t)) continue;
    counts.flags += 1;
    if (navSrc && !navFlags.has(t)) failures.push(`${file}: unknown navigator flag ${t} in: ${frag}`);
  }
}

const mdFiles = (await readdir(skillDir)).filter((f) => f.endsWith('.md'));
for (const f of mdFiles) {
  const md = await readFile(join(skillDir, f), 'utf-8');
  for (const frag of codeFragments(md)) {
    for (const tok of frag.split(/\s+/)) {
      await checkPathToken(tok.replace(/^["'(]+/, ''), f);
      await checkDottedAttr(tok.replace(/^["'(]+/, ''), f);
    }
    checkPaCommand(frag, f);
    checkNavigatorCommand(frag, f);
  }
}

console.log(`checked ${counts.paths} paths (${counts.tests} test ids), ${counts.attrs} module attributes, ` +
  `${counts.commands} bin/pa commands, ${counts.flags} flags in ${mdFiles.length} files`);
if (failures.length) {
  console.log(`${failures.length} problem(s):`);
  for (const x of failures) console.log(`  - ${x}`);
  process.exit(1);
}
console.log('all references resolve');
