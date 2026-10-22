// Pure type definitions. No runtime dependencies.

/** Shape of the script's `export const meta = {...}` (must be a plain literal). */
export type WorkflowMeta = {
  name: string
  description: string
  resultSchema?: object
  whenToUse?: string
  phases?: Array<{ title: string; detail?: string }>
}

export type WorkflowSubagentOverride = {
  /** Configured model id sent to the API. */
  model: string
  /** override replaces script opts.model; default only fills a missing opts.model. */
  mode?: 'override' | 'default'
}

/** Parameters passed by agent() to the AgentRunner. */
export type AgentRunParams = {
  prompt: string
  /** JSON Schema; when provided, agent returns a validated object instead of text. */
  schema?: object
  model?: string
  allowedTools?: string[]
  /** Display-only; not part of the journal key. */
  label?: string
  /** Display-only; not part of the journal key. */
  phase?: string
}

/** Delivery evidence retained for validation and run inspection. */
export type DeliveryAudit = {
  messageId?: string
  rawText?: string
  deliveredOutput?: unknown
  finishReason?: string | null
  validationErrors?: string[]
}

export type AgentRunResult =
  | {
      kind: 'ok'
      delivery?: DeliveryAudit
      output: string | object
      usage: { outputTokens: number }
      /** The actually-resolved model id (display-only). */
      model?: string
      /** Number of tool calls during the agent run. */
      toolCount?: number
      /** Total context tokens at completion. */
      tokenCount?: number
    }
  | { kind: 'skipped' }
  | {
      kind: 'dead'
      delivery?: DeliveryAudit
      /**
       * Cause-of-death classification for log aggregation / post-hoc auditing. Optional for backward compatibility with old journals.
       * - no-structured-output: agent finished but finalize content has no StructuredOutput (neither called tools nor produced JSON in text)
       * - runagent-threw: runAgent threw a non-abort error (API failure / context overflow / runtime error)
       * - unknown: unclassified (compatible with old backends / third-party adapters)
       */
      reason?:
        | 'no-structured-output'
        | 'runagent-threw'
        | 'unknown'
        | 'invalid-output'
        | 'incomplete-output'
        | 'delivery-mismatch'
      /** Detail (error message / text preview) for logs; not shown to end users. */
      detail?: string
    }

/** A single record in the journal. seq = agent() call sequence number; read() re-sorts by it to stabilize resume. */
export type JournalEntry = {
  key: string
  /** agent() call order (from agentIdSeq; monotonically increasing across sub-workflows). */
  seq: number
  result: AgentRunResult
  attempts?: AgentRunResult[]
  /** Actual runtime route used for this result; excluded from the deterministic call key. */
  execution?: { model?: string }
}

/** Progress events. All variants carry runId so the adapter can route to the corresponding task (multiple concurrent workflows). */
export type ProgressEvent =
  | {
      type: 'run_started'
      runId: string
      workflowName: string
      meta: WorkflowMeta | null
    }
  | { type: 'phase_started'; runId: string; phase: string }
  | { type: 'phase_done'; runId: string; phase: string }
  | {
      type: 'agent_started'
      runId: string
      agentId: number
      label?: string
      phase?: string
    }
  | {
      type: 'agent_done'
      runId: string
      agentId: number
      label?: string
      phase?: string
      result: AgentRunResult
    }
  | { type: 'log'; runId: string; message: string }
  | {
      type: 'run_done'
      runId: string
      status: 'completed' | 'failed' | 'killed'
      executionStatus?: 'completed' | 'failed' | 'killed'
      resultStatus?: 'valid' | 'invalid' | 'not_checked'
      resultErrors?: string[]
      returnValue?: unknown
      error?: string
    }

/** Engine run result. */
export type WorkflowRunResult = {
  executionStatus?: 'completed' | 'failed' | 'killed'
  resultStatus?: 'valid' | 'invalid' | 'not_checked'
  resultErrors?: string[]
  status: 'completed' | 'failed' | 'killed'
  returnValue?: unknown
  error?: string
}
