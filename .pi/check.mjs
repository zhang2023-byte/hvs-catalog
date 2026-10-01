// Offline compatibility gate. This never launches a child or asks a model.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { loadPi, openPi } from './pi-host.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const json = p => JSON.parse(fs.readFileSync(p, 'utf8'));
const sorted = xs => [...xs].sort();

// Project-owned report contract; plugin acceptance/exit proof is checked separately.
export function validateReview(report, schema, check) {
  assert(check(schema, report), 'Review JSON does not match .pi/review.schema.json');
  const mustFix = report.findings.some(f => f.severity === 'must_fix');
  assert(!(report.verdict === 'pass' && mustFix), 'pass cannot contain must_fix');
  assert(!(report.verdict === 'changes_required' && !mustFix), 'changes_required needs a must_fix finding');
  assert(report.validation.trim(), 'Validation evidence cannot be whitespace');
  return report.verdict;
}

// Check project invariants, not an exact snapshot of every upstream field.
// Additive capability fields are allowed; missing required fields fail closed.
export function validateCapabilities(capabilities, profiles) {
  assert(Array.isArray(capabilities?.agents), 'Missing public agentCapabilities.agents');
  for (const [name, profile] of Object.entries(profiles)) {
    const matches = capabilities.agents.filter(a => a.name === name);
    assert.equal(matches.length, 1, `Expected exactly one ${name}`);
    const agent = matches[0];
    assert.equal(agent.executable, true, `${name} is not executable`);
    assert.equal(agent.source, 'project');
    assert.equal(agent.runner.type, 'pi');
    assert.equal(agent.model.value, profile.model);
    assert.equal(agent.model.thinking, profile.thinking);
    assert.equal(agent.tools.ambient, false);
    assert.deepEqual(sorted(agent.tools.names), sorted(profile.tools));
    assert.deepEqual(agent.tools.mcpDirectTools, []);
    assert.deepEqual(agent.extensions.names, []);
    assert.equal(agent.execution.defaultAsync, true);
    assert.equal(agent.acceptance.role, profile.acceptanceRole);
    if (profile.acceptanceRole === 'writer') {
      assert.equal(agent.acceptance.policy.level, 'checked');
      assert.equal(agent.acceptance.policy.review, false);
    } else {
      const policy = agent.acceptance.policy;
      assert.equal(typeof policy === 'string' ? policy : policy?.level, 'attested', 'Reviewer must explicitly require native attestation');
    }
  }
}

// Compare the SDK-resolved values, not the unresolved modelOverrides object.
export function validateCompaction(effective, configured, model) {
  const key = `${model.provider}/${model.id}`;
  const override = configured.modelOverrides?.[key] ?? {};
  assert.deepEqual(effective, {
    enabled: configured.enabled,
    reserveTokens: override.reserveTokens ?? configured.reserveTokens,
    keepRecentTokens: override.keepRecentTokens ?? configured.keepRecentTokens,
  }, `Unexpected effective compaction for ${key}`);
  const threshold = model.contextWindow - effective.reserveTokens;
  assert(threshold > effective.keepRecentTokens, `${key}: no room after compaction`);
  if (key === 'zai-coding-cn/glm-5.3') {
    assert.equal(threshold, 300000, 'GLM registry changed: review the 300k threshold');
  }
  return { ...effective, contextWindow: model.contextWindow, threshold };
}

// Required collaboration packages plus the operator's optional notification package.
export function validateExtensions(extensions) {
  const names = extensions.map(extension => {
    // Skip module-only manifests (e.g. {"type":"module"}) to find package identity.
    let dir = path.dirname(extension.path);
    while (dir !== path.dirname(dir)) {
      const manifest = path.join(dir, 'package.json');
      if (fs.existsSync(manifest)) {
        const name = json(manifest).name;
        if (typeof name === 'string' && name) return name;
      }
      dir = path.dirname(dir);
    }
    throw new Error(`Cannot identify loaded extension: ${extension.path}`);
  });
  const notify = '@bacnh85/pi-notify';
  assert.deepEqual(sorted(names.filter(name => name !== notify)), ['pi-subagents', 'pi-web-access']);
  assert(names.filter(name => name === notify).length <= 1, 'Notification extension loaded more than once');
  return names;
}

export async function checkProject() {
  assert(!process.env.PI_SUBAGENT_CHILD, 'Run the public-tool integration check in the parent Pi environment, not inside a restricted child. Do not strip child-safety variables; children may run check.test.mjs and review the saved parent check report.');
  const host = await openPi(root);
  try {
    await host.enableSubagents();
    const settings = json(path.join(root, '.pi/settings.json'));
    assert.equal(settings.subagents.disableBuiltins, true);
    assert.equal(settings.subagents.watchdog.enabled, false);
    assert.equal(settings.subagents.watchdog.children.enabled, false);
    assert.equal(settings.subagents.modelScope.enforce, true);
    assert.equal(settings.subagents.modelScope.strict, true);
    const profiles = {};
    for (const name of ['hvs-implementer', 'hvs-simplifier', 'hvs-reviewer']) {
      const { frontmatter: f } = host.sdk.parseFrontmatter(fs.readFileSync(path.join(root, '.pi/agents', `${name}.md`), 'utf8'));
      assert.equal(f.name, name);
      assert.equal(f.defaultContext, 'fresh');
      assert.equal(f.inheritSkills, false);
      assert.equal(f.inheritProjectContext, true);
      assert.equal(f.systemPromptMode, 'append');
      assert.equal(f.defaultProgress, false);
      assert.equal(f.async, true);
      assert(!Object.hasOwn(f, 'fallbackModels'), 'Remove obsolete fallbackModels');
      assert(!Object.hasOwn(f, 'completionGuard'), 'Remove obsolete completionGuard');
      profiles[name] = { ...f, tools: typeof f.tools === 'string' ? f.tools.split(',').map(s => s.trim()) : f.tools };
    }
    assert.deepEqual(sorted(profiles['hvs-reviewer'].tools), ['bash', 'read']);
    for (const name of ['hvs-implementer', 'hvs-simplifier']) {
      assert.deepEqual(sorted(profiles[name].tools), ['bash', 'edit', 'read', 'write']);
      assert.equal(profiles[name].acceptanceRole, 'writer');
    }
    assert.equal(profiles['hvs-reviewer'].acceptanceRole, 'read-only');
    assert.equal(profiles['hvs-implementer'].model, 'zai-coding-cn/glm-5.3');
    assert.equal(profiles['hvs-simplifier'].model, 'openai-codex/gpt-6.1-sol');
    assert.equal(profiles['hvs-reviewer'].model, 'openai-codex/gpt-6.1-sol');
    const childModels = [...new Set(Object.values(profiles).map(p => p.model))];
    assert.deepEqual(sorted(settings.subagents.modelScope.allow), sorted(childModels));
    const listing = await host.call('subagent', { action: 'list', agentScope: 'project', capabilities: true });
    validateCapabilities(listing.details?.agentCapabilities, profiles);
    const extensionNames = validateExtensions(host.loader.getExtensions().extensions);
    assert.deepEqual(host.loader.getSkills().skills.map(s => s.name), []);
    assert.deepEqual(host.loader.getPrompts().prompts.map(p => p.name), ['hvs-work']);
    for (const key of ['defaultProvider', 'defaultModel', 'defaultThinkingLevel', 'enabledModels']) {
      assert(!Object.hasOwn(settings, key), `Parent model selection belongs to the operator, not project ${key}`);
    }
    const modelRuntime = await host.sdk.ModelRuntime.create();
    const compaction = {};
    for (const key of childModels) {
      const slash = key.indexOf('/');
      const model = modelRuntime.getModel(key.slice(0, slash), key.slice(slash + 1));
      assert(model, `Missing model registry entry: ${key}`);
      compaction[key] = validateCompaction(host.settings.getCompactionSettings(model), settings.compaction, model);
    }
    assert.equal(host.settings.getRetrySettings().maxRetries, settings.retry.maxRetries);
    assert.equal(host.settings.getProviderRetrySettings().maxRetries, 0);
    // Required management/call capability is tested through the real public tool.
    for (const name of Object.keys(profiles)) await host.call('subagent', {action:'get', agent:name});
    await host.call('subagent', {action:'children.list'});
    await host.call('subagent', {action:'validate', workflowScript:'return runs.run("probe", {label:"Probe protocol", agent:"hvs-implementer", task:"Not executed", context:"fresh"});'});
    const installations = {};
    for (const [scope, dir] of Object.entries({ user: path.join(host.agentDir, 'npm'), project: path.join(root, '.pi/npm') })) {
      installations[scope] = {};
      const packageFile = path.join(dir, 'package.json');
      if (!fs.existsSync(packageFile)) continue;
      for (const name of Object.keys(json(packageFile).dependencies || {})) {
        installations[scope][name] = json(path.join(dir, 'node_modules', name, 'package.json')).version;
      }
    }
    return {
      checkedAt: new Date().toISOString(), pi: host.version, installations,
      inputHashes: Object.fromEntries([
        '.pi/settings.json', '.pi/agents/hvs-implementer.md', '.pi/agents/hvs-simplifier.md', '.pi/agents/hvs-reviewer.md',
        '.pi/review.schema.json', '.pi/pi-host.mjs', '.pi/check.mjs',
      ].map(file => [file, createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex')])),
      result: 'passed', scope: 'offline public SDK/tool contract; no child or model calls',
      resources: { extensions: extensionNames, skills: [], prompts: ['hvs-work'] },
      agents: listing.details.agentCapabilities.agents,
      compaction,
      unverified: ['real model calls', 'retained-session continuation', 'provider/compaction faults', 'parent process restart'],
    };
  } finally { await host.close(); }
}

async function main() {
  const args = process.argv.slice(2);
  if (args[0] === '--review' && args.length === 2) {
    const { value } = await loadPi();
    const verdict = validateReview(json(path.resolve(args[1])), json(path.join(root, '.pi/review.schema.json')), value.Check);
    console.log(JSON.stringify({ verdict, scope: 'report format and consistency only; verify native acceptance, exit proof and evidence separately' }));
    if (verdict !== 'pass') process.exitCode = 1;
    return;
  }
  assert(args.length === 0 || (args[0] === '--output' && args.length === 2), 'Usage: node .pi/check.mjs [--output FILE | --review FILE]');
  const output = args[0] === '--output' ? path.resolve(args[1]) : undefined;
  if (output) {
    assert(output.startsWith(path.join(root, '.work') + path.sep), 'Check output must stay under this repository .work/');
    fs.mkdirSync(path.dirname(output), { recursive: true });
  }
  const save = report => { if (output) fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n'); };
  // Invalidate prior success BEFORE checking: a failed rerun must not leave a stale pass.
  save({ result: 'checking', checkedAt: new Date().toISOString() });
  try {
    const report = await checkProject();
    save(report);
    console.log(JSON.stringify(report, null, 2));
  } catch (error) {
    save({ result: 'failed', checkedAt: new Date().toISOString(), error: error.message });
    throw error;
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch(error => { console.error(`Pi compatibility check FAILED: ${error.message}`); process.exitCode = 1; });
}
