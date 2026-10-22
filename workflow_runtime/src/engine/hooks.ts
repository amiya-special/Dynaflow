import { MAX_ITEMS_PER_CALL, MAX_TOTAL_AGENTS } from '../constants.js'
import type {
  AgentRunParams,
  AgentRunResult,
  JournalEntry,
  ProgressEvent,
  WorkflowSubagentOverride,
} from '../types.js'
import type { EngineContext } from './context.js'
import { WorkflowAbortedError, WorkflowError } from './errors.js'
import { validateAgentDelivery } from './deliveryValidation.js'
import { validateAgainstSchema } from './structuredOutput.js'
import { agentCallKey } from './journal.js'
import type { WorkflowHooks } from './script.js'

/** Sub-workflow executor for the workflow() hook (injected by runWorkflow to avoid circular dependencies). */
export type SubWorkflowRunner = (opts: {
  script: string
  args?: unknown
}) => Promise<unknown>

type HookProgressInit =
  | { type: 'phase_started'; phase: string }
  | { type: 'phase_done'; phase: string }
  | { type: 'agent_started'; agentId: number; label?: string; phase?: string }
  | {
      type: 'agent_done'
      agentId: number
      label?: string
      phase?: string
      result: AgentRunResult
    }
  | { type: 'log'; message: string }

export function makeHooks(
  ctx: EngineContext,
  runSubWorkflow: SubWorkflowRunner,
): WorkflowHooks {
  // All progress events auto-inject runId so the adapter can route them to the corresponding task (multiple concurrent workflows)
  const emit = (init: HookProgressInit): void => {
    ctx.ports.progressEmitter.emit({
      runId: ctx.runId,
      ...init,
    } as ProgressEvent)
  }

  const agent: WorkflowHooks['agent'] = async (prompt, opts = {}) => {
    const r = ctx.resources
    if (r.agentCountBox.value >= MAX_TOTAL_AGENTS) {
      throw new WorkflowError(
        `workflow exceeds total agent cap (${MAX_TOTAL_AGENTS})`,
      )
    }

    // Assign a unique id to each agent() call (including journal hits); stamp started/done so the reducer can associate them precisely
    const agentId = r.agentIdSeq.value++

    // Keep script params separate from execution params. The deterministic
    // journal key describes the script call; the configured model is applied
    // only after replay misses.
    const params: AgentRunParams = { prompt, ...opts }
    if (params.schema) validateAgainstSchema(null, params.schema)
    const key = agentCallKey(prompt, params)
    const label = opts.label as string | undefined
    const phase =
      (opts.phase as string | undefined) ?? ctx.currentPhase ?? undefined

    // Journal hit -> return cached result directly
    if (!ctx.journalInvalidated && ctx.journalIndex < ctx.journal.length) {
      const entry = ctx.journal[ctx.journalIndex]!
      if (entry.key === key) {
        ctx.journalIndex++
        const replayResult = validateAgentDelivery(entry.result, params)
        ctx.journal[ctx.journalIndex - 1] = { ...entry, result: replayResult }
        emit({
          type: 'agent_done',
          agentId,
          label,
          phase,
          result: replayResult,
        })
        return resultToOutput(replayResult)
      }
      // Divergence: discard subsequent journal entries; everything from here on runs live
      ctx.journalInvalidated = true
      ctx.journal = ctx.journal.slice(0, ctx.journalIndex)
      await ctx.ports.journalStore.truncate(ctx.runId)
    }

    let release: () => void
    try {
      release = await ctx.resources.semaphore.acquire(ctx.signal)
    } catch {
      // Queued wait during abort: the semaphore already removed the waiter and did not consume a permit
      throw new WorkflowAbortedError()
    }
    try {
      if (ctx.signal.aborted) throw new WorkflowAbortedError()
      // Budget check inside the semaphore critical section: a queued waiter sees the latest spent when woken,
      // otherwise N waiters enqueued while spent=0 all pass the check and overspend on wake-up without re-check.
      // Journal-hit path does not charge budget and needs no check.
      r.budget.assertCanSpend()

      ctx.resources.agentCountBox.value++
      emit({ type: 'agent_started', agentId, label, phase })
      const executionParams = ctx.subagentOverride
        ? applySubagentOverride(params, ctx.subagentOverride)
        : params
      const attempts: AgentRunResult[] = []
      const invokeBackend = async (): Promise<AgentRunResult> => {
        try {
          const raw = await ctx.ports.agentRunner.runAgentToResult(
            executionParams,
            ctx.host,
          )
          const checked = validateAgentDelivery(raw, executionParams)
          attempts.push(checked)
          return checked
        } catch (error) {
          if (!(error instanceof WorkflowAbortedError))
            attempts.push({
              kind: 'dead',
              reason: 'runagent-threw',
              detail: error instanceof Error ? error.message : String(error),
            })
          throw error
        }
      }

      // Auto-retry once on failure: dead (terminal API error after retries) or a non-abort throw
      // both get one retry chance; WorkflowAbortedError (kill) is not retried — it is the user's intent.
      // If retry still fails: dead stays dead; a throw degrades to dead (one agent must not take down the workflow).
      // budget is not double-charged: dead does not call addOutputTokens; retry-ok charges once (at the final ok).
      // dead.reason is passed through to the log: no-structured-output (the agent's final text block did not produce plain-object JSON)
      // is a high-frequency cause of death; logging detail lets you immediately see what the agent last said.
      // Only log string details so malformed error records cannot break retry handling.
      let result: AgentRunResult
      try {
        result = await invokeBackend()
        if (
          result.kind === 'dead' &&
          ![
            'invalid-output',
            'incomplete-output',
            'delivery-mismatch',
          ].includes(result.reason ?? '')
        ) {
          const detailStr =
            typeof result.detail === 'string' ? result.detail : ''
          ctx.ports.logger.warn?.(
            `agent "${label ?? `#${agentId}`}" returned dead` +
              (result.reason ? ` (${result.reason})` : '') +
              (detailStr ? `: ${detailStr.slice(0, 150)}` : '') +
              '; retrying once',
          )
          result = await invokeBackend()
        }
      } catch (e) {
        if (e instanceof WorkflowAbortedError) throw e
        const eMsg = e instanceof Error ? e.message : String(e)
        ctx.ports.logger.warn?.(
          `agent "${label ?? `#${agentId}`}" threw (${eMsg}); retrying once`,
        )
        try {
          result = await invokeBackend()
        } catch (e2) {
          if (e2 instanceof WorkflowAbortedError) throw e2
          // Retry still threw: degrade to dead (keep the workflow going; hooks.agent returns null)
          result = {
            kind: 'dead',
            reason: 'runagent-threw',
            detail: e2 instanceof Error ? e2.message : String(e2),
          }
        }
      }
      if (result.kind === 'ok') {
        ctx.resources.budget.addOutputTokens(result.usage.outputTokens)
      }
      if (result.kind === 'dead') {
        emit({
          type: 'log',
          message: `agent #${agentId} delivery failed: ${result.reason}: ${result.detail ?? ''}`,
        })
      }
      emit({ type: 'agent_done', agentId, label, phase, result })

      const entry: JournalEntry = {
        key,
        seq: agentId,
        result,
        attempts,
        ...(executionParams.model && { execution: { model: executionParams.model } }),
      }
      // Key point: push order = completion order (not call order); read() already re-sorts by seq,
      // so during resume the call order aligns with the journal order and the key index stays stable.
      ctx.journal.push(entry)
      ctx.journalIndex++
      await ctx.ports.journalStore.append(ctx.runId, entry)
      return resultToOutput(result)
    } finally {
      release()
    }
  }

  const parallel: WorkflowHooks['parallel'] = async thunks => {
    if (thunks.length > MAX_ITEMS_PER_CALL) {
      throw new WorkflowError(
        `parallel exceeds the per-call items cap (${MAX_ITEMS_PER_CALL})`,
      )
    }
    return Promise.all(
      thunks.map(async (t, i) => {
        try {
          return await t()
        } catch (e) {
          // The "null on error" contract is unchanged, but it should log — otherwise the workflow author cannot locate why an agent failed
          ctx.ports.logger.warn?.(
            `parallel thunk #${i} failed: ${(e as Error).message}`,
          )
          return null
        }
      }),
    )
  }

  const pipeline: WorkflowHooks['pipeline'] = async <T, R>(
    items: readonly T[],
    ...stages: Array<
      (prev: unknown, item: T, index: number) => Promise<unknown>
    >
  ): Promise<Array<R | null>> => {
    if (items.length > MAX_ITEMS_PER_CALL) {
      throw new WorkflowError(
        `pipeline exceeds the per-call items cap (${MAX_ITEMS_PER_CALL})`,
      )
    }
    return Promise.all(
      items.map(async (item, index): Promise<R | null> => {
        try {
          let prev: unknown = item
          for (const stage of stages) {
            prev = await stage(prev, item, index)
          }
          return prev as R
        } catch (e) {
          ctx.ports.logger.warn?.(
            `pipeline item #${index} failed: ${(e as Error).message}`,
          )
          return null
        }
      }),
    )
  }

  const phase: WorkflowHooks['phase'] = title => {
    if (ctx.currentPhase) {
      emit({ type: 'phase_done', phase: ctx.currentPhase })
    }
    ctx.currentPhase = title
    emit({ type: 'phase_started', phase: title })
  }

  const log: WorkflowHooks['log'] = message => {
    emit({ type: 'log', message })
  }

  const workflow: WorkflowHooks['workflow'] = async input => {
    if (ctx.resources.depth >= 1) {
      throw new WorkflowError('workflow() nesting allows only one level')
    }
    if (!input || typeof input.script !== 'string' || !input.script.trim()) {
      throw new WorkflowError('workflow() requires an inline script: workflow({script, args})')
    }
    return runSubWorkflow({ script: input.script, args: input.args })
  }

  return { agent, parallel, pipeline, phase, log, workflow }
}

function applySubagentOverride(
  params: AgentRunParams,
  override: WorkflowSubagentOverride,
): AgentRunParams {
  const mode = override.mode ?? 'override'
  return {
    ...params,
    ...(mode === 'override' || !params.model ? { model: override.model } : {}),
  }
}

function resultToOutput(result: AgentRunResult): unknown {
  return result.kind === 'ok' ? result.output : null
}
