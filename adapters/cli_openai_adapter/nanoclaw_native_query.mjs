#!/usr/bin/env node
import { query } from '/app/nanoclaw-agent-runner/node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs';

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const key = argv[i];
    if (!key.startsWith('--')) continue;
    const name = key.slice(2);
    const value = argv[i + 1];
    if (value && !value.startsWith('--')) {
      out[name] = value;
      i += 1;
    } else {
      out[name] = '1';
    }
  }
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const prompt = args.prompt || '';
  const cwd = args.cwd || '/workspace/group';
  const resume = args.resume || '';
  const model = process.env.NANOCLAW_CLAUDE_MODEL || '';

  if (!prompt.trim()) {
    throw new Error('missing --prompt');
  }

  let sessionId = '';
  let answer = '';

  const options = {
    cwd,
    permissionMode: 'bypassPermissions',
    allowDangerouslySkipPermissions: true,
    maxTurns: 4,
    settingSources: ['user'],
  };
  if (resume) options.resume = resume;
  if (model) options.model = model;

  for await (const msg of query({ prompt, options })) {
    if (msg.type === 'system' && msg.subtype === 'init' && msg.session_id) {
      sessionId = msg.session_id;
    }
    if (msg.type === 'result' && msg.subtype === 'success') {
      const text = typeof msg.result === 'string' ? msg.result.trim() : '';
      if (text) {
        answer = text;
        break;
      }
    }
  }

  if (!answer) {
    throw new Error('no result from sdk query');
  }

  process.stdout.write(`${JSON.stringify({ answer, sessionId })}\n`);
}

main().catch((err) => {
  process.stderr.write(`${String(err?.message || err)}\n`);
  process.exit(1);
});
