// Pi integration boundary: public package entry + exported SDK/tool APIs only.
// No pi-subagents/src imports, model prompts, installs, or global settings writes.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

export async function loadPi() {
  const packageRoot = process.env.HVS_PI_PACKAGE_ROOT || path.join(
    execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim(),
    '@earendil-works/pi-coding-agent',
  );
  const manifestPath = path.join(packageRoot, 'package.json');
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  if (manifest.name !== '@earendil-works/pi-coding-agent') throw new Error('HVS_PI_PACKAGE_ROOT must identify the Pi package.');
  // Documented host hint for SDK processes whose argv is not the pi executable.
  // Process-local only; preserve any operator-supplied override.
  process.env.PI_SUBAGENTS_PI_CODING_AGENT_PACKAGE_ROOT ??= packageRoot;
  const entry = manifest.exports?.['.']?.import;
  if (typeof entry !== 'string') throw new Error('Pi public ESM entry unavailable; update .pi/pi-host.mjs, not the workflow.');
  const sdk = await import(pathToFileURL(path.resolve(packageRoot, entry)));
  const require = createRequire(manifestPath);
  const value = await import(pathToFileURL(require.resolve('typebox/value')));
  return { sdk, value, version: manifest.version };
}

export async function openPi(cwd, { sessionManager, extensionFactories = [] } = {}) {
  const host = await loadPi();
  const { sdk } = host;
  const agentDir = sdk.getAgentDir();
  const trust = new sdk.ProjectTrustStore(agentDir).getEntry(cwd);
  if (trust?.decision !== true || trust.path !== fs.realpathSync(cwd)) {
    throw new Error('This exact project must already be trusted in Pi; the checker does not grant trust.');
  }
  const settings = sdk.SettingsManager.create(cwd, agentDir, { projectTrusted: true });
  const loader = new sdk.DefaultResourceLoader({ cwd, agentDir, settingsManager: settings, extensionFactories });
  await loader.reload();
  const errors = loader.getExtensions().errors;
  if (errors.length) throw new Error(`Extension loading failed: ${JSON.stringify(errors)}`);
  const { session } = await sdk.createAgentSession({
    cwd, settingsManager: settings, resourceLoader: loader,
    sessionManager: sessionManager || sdk.SessionManager.inMemory(),
  });
  // Check/test host never spends a parent model turn, including notification wakes.
  // Native background children use their own sessions and normal providers.
  session.agent.streamFunction = () => { throw new Error('Model calls are disabled in the Pi check host.'); };
  try {
    await session.bindExtensions({});
  } catch (error) {
    session.dispose();
    throw error;
  }
  async function call(name, params) {
    const runner = session.extensionRunner;
    const tool = runner.getToolDefinition(name);
    if (!tool) throw new Error(`Required public tool unavailable: ${name}. Stop and update the integration boundary.`);
    if (!host.value.Check(tool.parameters, params)) throw new Error(`Unsupported ${name} parameters: ${JSON.stringify(params)}`);
    const result = await tool.execute(crypto.randomUUID(), params, undefined, undefined, runner.createContext());
    if (result.isError) throw new Error(`${name} failed: ${JSON.stringify(result)}`);
    return result;
  }
  return {
    ...host, agentDir, settings, loader, session, call,
    async enableSubagents() {
      if (session.extensionRunner.getToolDefinition('subagents_enable')) await call('subagents_enable', {});
      if (!session.extensionRunner.getToolDefinition('subagent')) throw new Error('No subagent tool after activation.');
    },
    async close() {
      try { await session.extensionRunner.emit({ type: 'session_shutdown' }); }
      finally { session.dispose(); }
    },
  };
}
