import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// 只修补已核对的 DSH 版本；上游升级后必须重新审查 provider 和 Session 契约。
const root = '/usr/local/lib/node_modules/@deepseek-ai/dsh';
const manifest = JSON.parse(readFileSync(`${root}/package.json`, 'utf8'));
assert.equal(manifest.version, '0.2.1-rc.3');
for (const name of ['dsh-subagent-fork-in-process', 'dsh-session']) {
  const dependency = JSON.parse(readFileSync(`${root}/node_modules/@deepseek-ai/${name}/package.json`, 'utf8'));
  assert.equal(dependency.version, '0.2.1-rc.3', `${name} 版本不匹配`);
}

const target = `${root}/node_modules/@deepseek-ai/dsh-subagent-fork-in-process/lib/index.js`;
const original = readFileSync(target, 'utf8');
const workflowTarget = `${root}/node_modules/@deepseek-ai/dsh-workflow-ptc/lib/index.js`;
const workflowOriginal = readFileSync(workflowTarget, 'utf8');
const importAnchor = 'import z from "@deepseek-ai/schemastery";';
const applyAnchor = `function apply(ctx, config) {
	ctx.subagents.registerProvider(new ForkInProcessProvider(config.providerName));
}`;
const replacement = `// 同轮次快照复用 DSH Session 的平衡逻辑，不修改原有 fork provider。
class ForkInflightProvider extends ForkInProcessProvider {
	prepareContinuable(request) {
		const events = request.parent.session.snapshotEvents();
		const last = events.at(-1);
		return Promise.resolve(last === undefined ? {} : { seed: buildForkSeed(events, last.seq) });
	}
}
function apply(ctx, config) {
	ctx.subagents.registerProvider(new ForkInProcessProvider(config.providerName));
	ctx.subagents.registerProvider(new ForkInflightProvider("fork-inflight"));
}`;

for (const anchor of [importAnchor, applyAnchor]) {
  assert.equal(original.split(anchor).length, 2, `上游包结构变化：${anchor}`);
}
const patched = original
  .replace(importAnchor, `${importAnchor}\nimport { buildForkSeed } from "@deepseek-ai/dsh-session";`)
  .replace(applyAnchor, replacement);
writeFileSync(target, patched);

// rc.3 的 Workflow 默认配置仍解析为 spawn；普通 DSH 必须显式映射到同轮次继承 provider。
const workflowAnchor = 'const provider = override ?? configured;';
assert.equal(workflowOriginal.split(workflowAnchor).length, 2, '上游 Workflow provider 解析结构变化');
writeFileSync(workflowTarget, workflowOriginal.replace(
  workflowAnchor,
  'const provider = override ?? (configured === "spawn" && ctx.subagents.getProvider("fork-inflight") !== void 0 ? "fork-inflight" : configured);',
));

// 构建期调用真实 provider，检查在途工具结果进入子会话并被平衡关闭。
const plugin = await import(pathToFileURL(target).href);
const providers = new Map();
plugin.apply({ subagents: { registerProvider: provider => providers.set(provider.name, provider) } }, { providerName: 'fork' });
assert(providers.has('fork'));
const inflight = providers.get('fork-inflight');
assert(inflight);
const events = [
  { type: 'turn/start', seq: 0, time: 1, data: { turn: 1 } },
  { type: 'step/start', seq: 1, time: 1, data: { turn: 1, step: 1 } },
  { type: 'assistant/message', seq: 2, time: 1, data: { turn: 1, step: 1, message: { role: 'assistant', content: [{ type: 'tool-call', id: 'read-1', name: 'read', arguments: '{}' }] } } },
  { type: 'tool/call', seq: 3, time: 1, data: { turn: 1, step: 1, callId: 'read-1', name: 'read', arguments: '{}' } },
  { type: 'tool/result', seq: 4, time: 1, surfaceOp: 'append', data: { turn: 1, step: 1, message: { role: 'tool', source: { kind: 'tool', callId: 'read-1' }, toolCallId: 'read-1', content: [{ type: 'text', text: '已读取的证据' }] } } },
  { type: 'step/end', seq: 5, time: 1, data: { turn: 1, step: 1 } },
  { type: 'step/start', seq: 6, time: 1, data: { turn: 1, step: 2 } },
  { type: 'assistant/message', seq: 7, time: 1, data: { turn: 1, step: 2, message: { role: 'assistant', content: [{ type: 'tool-call', id: 'workflow-1', name: 'workflow', arguments: '{}' }] } } },
  { type: 'tool/call', seq: 8, time: 1, data: { turn: 1, step: 2, callId: 'workflow-1', name: 'workflow', arguments: '{}' } },
];
const prepared = await inflight.prepareContinuable({ parent: { session: { snapshotEvents: () => events } } });
assert.equal(prepared.seed[4].data.message.content[0].text, '已读取的证据');
assert(prepared.seed.some(event => event.type === 'tool/result' && event.data.message.source.callId === 'workflow-1'));
assert.equal(prepared.seed.at(-1).type, 'turn/end');
assert.equal(prepared.seed.at(-1).data.reason.kind, 'forked');
console.log('fork-inflight provider 已注册，当前轮次快照已平衡');
