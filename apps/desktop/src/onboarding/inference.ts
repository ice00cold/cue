/**
 * Start waits until the first chat can actually be answered (D23): `setup.status` (the backend holds
 * it up to ~12 s while the free account is made), then `setup.runtime_check`. A terminal free-tier
 * failure ends the wait at once; anything else retries until the deadline.
 */

import type { SetupRuntimeCheckResult, SetupStatusResult } from '@hermes/shared'

import { $freeTierStatus, freeTierSetupFailure } from '@/store/free-tier'

import type { OnboardingRequester } from './due'

export const START_WAIT_MS = 25_000

const RETRY_GAP_MS = 1_500

export type InferenceWait = { ok: true } | { ok: false; reason: 'terminal' | 'timeout' }

export interface InferenceClock {
  now: () => number
  sleep: (ms: number) => Promise<void>
}

export const realClock: InferenceClock = {
  now: () => Date.now(),
  sleep: ms => new Promise(resolve => setTimeout(resolve, ms))
}

function terminalFailure(status: null | SetupStatusResult): boolean {
  const freeTier = freeTierSetupFailure($freeTierStatus.get())

  return Boolean((freeTier && !freeTier.retryable) || (status?.error_code && status.retryable === false))
}

// `manage_connections` caches its availability for 30 s, so a free-tier chat must not start before the account exists.
function statusReady(status: null | SetupStatusResult): boolean {
  return Boolean(status?.ready && status.provider_configured && (status.free_tier_account || !status.free_tier_route))
}

export async function waitForInference(
  request: OnboardingRequester,
  { clock = realClock, timeoutMs = START_WAIT_MS }: { clock?: InferenceClock; timeoutMs?: number } = {}
): Promise<InferenceWait> {
  const deadline = clock.now() + timeoutMs

  while (clock.now() < deadline) {
    const status = await request<SetupStatusResult>('setup.status').catch(() => null)

    if (terminalFailure(status)) {
      return { ok: false, reason: 'terminal' }
    }

    if (statusReady(status)) {
      const runtime = await request<SetupRuntimeCheckResult>('setup.runtime_check').catch(() => null)

      if (runtime?.ok) {
        return { ok: true }
      }
    }

    await clock.sleep(RETRY_GAP_MS)
  }

  return { ok: false, reason: 'timeout' }
}
