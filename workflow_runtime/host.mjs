// Host adapter for the bundled Workflow engine. Model credentials stay in Python.
import {createInterface} from 'node:readline';
import {readFile} from 'node:fs/promises';
import {runWorkflow} from './generated/engine/runWorkflow.js';
import {createFileJournalStore} from './generated/engine/journal.js';
import {createHostHandle} from './generated/ports.js';
import {workflowInputSchema} from './tool.mjs';
const input = JSON.parse(await readFile(process.argv[2], 'utf8'));
const pending = new Map();
let nextId = 0;
const lines = createInterface({input: process.stdin});
lines.on('line', line => {
  try { const reply = JSON.parse(line); pending.get(reply.id)?.(reply.result); pending.delete(reply.id); }
  catch { process.exitCode = 2; }
});
function emit(value) { process.stdout.write(JSON.stringify(value) + '\n'); }
const controller = new AbortController();
const ports = {
  agentRunner: {runAgentToResult(params) {
    const id = ++nextId;
    return new Promise(resolve => {pending.set(id, resolve); emit({type: 'agent_request', id, params});});
  }},
  journalStore: createFileJournalStore(input.workDir),
  progressEmitter: {emit: event => emit({type: 'progress', event})},
  logger: {warn: message => emit({type: 'warning', message})},
};
try {
  const args = workflowInputSchema.parse(input.invocation);
  const result = await runWorkflow({script: args.script, args: args.args,
    runId: input.runId, cwd: input.workDir, ports, host: createHostHandle(null),
    signal: controller.signal, budgetTotal: null, maxConcurrency: args.maxConcurrency,
    resume: Boolean(input.resume), scriptChanged: Boolean(input.scriptChanged),
    subagentOverride: {model: input.subagentModel, mode: 'override'},
  });
  emit({type: 'result', result});
} catch (error) { emit({type: 'result', result: {status: 'failed', resultStatus: 'invalid', error: error.message}}); }
lines.close();
process.stdin.destroy();
