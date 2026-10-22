/** Default number of concurrent agent calls within one workflow. */
export const DEFAULT_MAX_CONCURRENCY = 3

/** Maximum user-configured number of concurrent agent calls. */
export const MAX_CONCURRENCY_CAP = 16

/** Total cap on agent() calls within a single workflow lifecycle. */
export const MAX_TOTAL_AGENTS = 1000

/** Items cap per single parallel()/pipeline() call. */
export const MAX_ITEMS_PER_CALL = 4096
