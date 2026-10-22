import type { AgentRunParams, AgentRunResult, JournalEntry, ProgressEvent } from './types.js'

/** Opaque context passed between the host and its agent runner. */
const HOST_HANDLE = Symbol('workflow.hostHandle')
export type HostHandle = { readonly [HOST_HANDLE]: unknown }

export function createHostHandle(bundle: unknown): HostHandle {
  return { [HOST_HANDLE]: bundle }
}

/** Execute one subagent through the host's configured API connection. */
export type AgentRunner = {
  runAgentToResult(params: AgentRunParams, host: HostHandle): Promise<AgentRunResult>
}

export type ProgressEmitter = {
  emit(event: ProgressEvent): void
}

export type JournalStore = {
  read(runId: string): Promise<JournalEntry[]>
  append(runId: string, entry: JournalEntry): Promise<void>
  truncate(runId: string): Promise<void>
}

export type Logger = {
  warn?(message: string): void
}

/** Runtime services supplied by the standalone host. */
export type WorkflowPorts = {
  agentRunner: AgentRunner
  progressEmitter: ProgressEmitter
  journalStore: JournalStore
  logger: Logger
}
