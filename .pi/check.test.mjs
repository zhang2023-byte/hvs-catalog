import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { loadPi } from './pi-host.mjs';
import { validateReview, validateCapabilities, validateCompaction } from './check.mjs';
const { value, sdk } = await loadPi();
const schema = JSON.parse(fs.readFileSync(new URL('./review.schema.json', import.meta.url), 'utf8'));
const good = { verdict:'pass', summary:'Synthetic format specimen', findings:[], validation:'Synthetic only: command evidence is checked separately', risks:[] };
const finding = {severity:'must_fix', file:'fixture.py', issue:'Synthetic defect', fix:'Repair fixture'};
for (const verdict of ['pass', 'blocked', 'changes_required']) {
  test(`valid ${verdict} report`, () => {
    const report = {...good, verdict, findings:verdict === 'changes_required' ? [finding] : []};
    assert.equal(validateReview(report, schema, value.Check), verdict);
  });
}
for (const [name, change] of Object.entries({
  'unknown verdict': {verdict:'PASS'},
  'unknown severity': {findings:[{...finding,severity:'critical'}]},
  'missing finding fields': {findings:[{severity:'must_fix'}]},
  'empty evidence': {validation:''},
  'whitespace evidence': {validation:'   '},
  'unexpected fields': {extra:true},
  'wrong risk type': {risks:[42]},
  'too many findings': {findings:Array(21).fill(finding)},
  'pass with must-fix': {findings:[finding]},
  'changes without findings': {verdict:'changes_required'},
})) test(`reject ${name}`, () => assert.throws(() => validateReview({...good,...change},schema,value.Check)));
test('reject missing report / missing verdict', () => {
  assert.throws(() => validateReview(undefined,schema,value.Check));
  const {verdict,...incomplete}=good;
  assert.throws(() => validateReview(incomplete,schema,value.Check));
});
const profile={model:'provider/model',thinking:'high',tools:['read','bash'],acceptanceRole:'read-only'};
const agent={name:'review',source:'project',executable:true,runner:{type:'pi'},model:{value:profile.model,thinking:profile.thinking},tools:{ambient:false,names:profile.tools,mcpDirectTools:[]},extensions:{names:[]},execution:{defaultAsync:true},acceptance:{role:'read-only',policy:{level:'attested'}}};
test('accept documented scalar attested policy', () => validateCapabilities({agents:[{...agent,acceptance:{role:'read-only',policy:'attested'}}]},{review:profile}));
test('allow additive upstream capability fields and tool reordering', () => validateCapabilities({agents:[{...agent,newField:true,tools:{...agent.tools,names:['bash','read']}}],futureMetadata:{}},{review:profile}));
for (const [name, change] of Object.entries({
  'missing executable': {executable:undefined},
  'disabled agent': {executable:false},
  'extra write tool': {tools:{...agent.tools,names:['read','bash','write']}},
  'ambient tools': {tools:{...agent.tools,ambient:true}},
  'unexpected extensions': {extensions:{names:['unexpected']}},
  'wrong model': {model:{value:'provider/other',thinking:'high'}},
  'wrong thinking': {model:{...agent.model,thinking:'low'}},
  'missing acceptance role': {acceptance:{}},
  'missing attestation policy': {acceptance:{role:'read-only'}},
  'disabled attestation': {acceptance:{role:'read-only',policy:{level:'none'}}},
})) test(`capabilities reject ${name}`, () => assert.throws(() => validateCapabilities({agents:[{...agent,...change}]},{review:profile})));
test('reject missing or duplicated required agents', () => {
  for (const agents of [[],[agent,agent]]) assert.throws(() => validateCapabilities({agents},{review:profile}));
  assert.throws(() => validateCapabilities({}, {review:profile}));
});
const compactionConfig = {
  enabled: true, reserveTokens: 32768, keepRecentTokens: 20000,
  modelOverrides: {'zai-coding-cn/glm-5.3': {reserveTokens: 700000}},
};
for (const [provider, id, contextWindow, threshold] of [
  ['zai-coding-cn', 'glm-5.3', 1000000, 300000],
  ['openai-codex', 'gpt-6-astra', 272000, 239232],
  ['openai-codex', 'gpt-6-sol', 272000, 239232],
]) test(`model-specific compaction: ${provider}/${id}`, () => {
  const settings = sdk.SettingsManager.inMemory({compaction: compactionConfig});
  const model = {provider, id, contextWindow};
  const report = validateCompaction(settings.getCompactionSettings(model), compactionConfig, model);
  assert.equal(report.threshold, threshold);
  assert.equal(report.keepRecentTokens, 20000);
});
test('compaction check rejects model metadata drift and unintended overrides', () => {
  const model = {provider:'zai-coding-cn', id:'glm-5.3', contextWindow:1048576};
  const effective = {enabled:true, reserveTokens:700000, keepRecentTokens:20000};
  assert.throws(() => validateCompaction(effective, compactionConfig, model), /registry changed/);
  assert.throws(() => validateCompaction({...effective, reserveTokens:32768}, compactionConfig, {...model, contextWindow:1000000}), /Unexpected effective/);
});
test('failed check replaces stale success and respects child restriction', () => {
  const root = fileURLToPath(new URL('../', import.meta.url));
  fs.mkdirSync(path.join(root, '.work'), {recursive:true});
  const dir = fs.mkdtempSync(path.join(root, '.work/pi-check-test-'));
  const report = path.join(dir, 'offline.json');
  try {
    fs.writeFileSync(report, JSON.stringify({result:'passed'}));
    const run = spawnSync(process.execPath, ['.pi/check.mjs','--output',report], {
      cwd:root, encoding:'utf8', timeout:10000,
      env:{...process.env, PI_OFFLINE:'1', PI_SUBAGENT_CHILD:'1'},
    });
    assert.equal(run.status,1);
    const failed=JSON.parse(fs.readFileSync(report,'utf8'));
    assert.equal(failed.result,'failed');
    assert.match(failed.error,/parent Pi environment/);
  } finally { fs.rmSync(dir, {recursive:true,force:true}); }
});
