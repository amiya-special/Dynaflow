import type { AgentRunParams, AgentRunResult } from '../types.js'
import { validateAgainstSchema } from './structuredOutput.js'

export function validateDelivery(value: unknown, schema?: object): string[] {
  if (value === undefined) return ['empty-output: undefined result']
  try {
    if (value !== undefined)
      JSON.stringify(value, (_key, item: unknown) => {
        if (
          ['undefined', 'function', 'symbol', 'bigint'].includes(typeof item) ||
          (typeof item === 'number' && !Number.isFinite(item))
        )
          throw new Error('non-JSON value')
        return item
      })
  } catch {
    return ['unserializable-output']
  }
  if (schema) {
    try {
      return validateAgainstSchema(value, schema).errors
    } catch (error) {
      return [
        `invalid-schema: ${error instanceof Error ? error.message : String(error)}`,
      ]
    }
  }
  const seen = new Set<object>()
  function hasContent(item: unknown): boolean {
    if (typeof item === 'string') return item.trim().length > 0
    if (typeof item === 'boolean') return true
    if (typeof item === 'number') return Number.isFinite(item)
    if (item === null || typeof item !== 'object' || seen.has(item))
      return false
    seen.add(item)
    return Object.values(item).some(hasContent)
  }
  try {
    // Reject non-JSON results rather than losing them during state/notification serialization.
    if (
      value === undefined ||
      JSON.stringify(value) === undefined ||
      !hasContent(value)
    ) {
      return ['empty-output: no nonempty deliverable value']
    }
  } catch {
    return ['unserializable-output']
  }
  return []
}

export function validateAgentDelivery(
  result: AgentRunResult,
  params: AgentRunParams,
): AgentRunResult {
  if (result.kind !== 'ok') return result
  const errors = validateDelivery(result.output, params.schema)
  if (!errors.length) return result
  return {
    kind: 'dead',
    reason: 'invalid-output',
    detail: errors.join('; '),
    delivery: {
      ...result.delivery,
      deliveredOutput: result.output,
      validationErrors: errors,
    },
  }
}
