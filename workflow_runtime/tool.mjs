import {readFile} from 'node:fs/promises';
import {z} from 'zod/v4';

export const workflowInputSchema = z.strictObject({
  script: z.string().min(1).optional().describe('Inline JavaScript. Required for a new run; omit on resume to reuse its saved script.'),
  args: z.unknown().optional().describe('JSON value exposed as args inside the script.'),
  resumeFromRunId: z.string().min(1).optional().describe('Task ID of a finished Workflow in this question session. Reuse its journal; change script to invalidate cached calls.'),
  description: z.string().optional().describe('Short description of this invocation.'),
  maxConcurrency: z.number().int().min(1).max(16).optional().describe('Concurrent subagent limit; default 3, maximum 16.'),
});

export async function workflowTool() {
  return {type: 'function', function: {
    name: 'Workflow',
    description: await readFile(new URL('./workflow_prompt.txt', import.meta.url), 'utf8'),
    parameters: z.toJSONSchema(workflowInputSchema),
  }};
}
