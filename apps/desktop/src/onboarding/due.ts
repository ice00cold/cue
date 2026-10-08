/**
 * Whether the questionnaire runs, and the one flag that says so (`onboarding.run` in the root
 * profile's config). `onboarding.state` answers `{run, eligible}`; an older backend answers another
 * shape under the same name or lacks the method, and both read as "not due" (version skew).
 */

import type { OnboardingRunStateResult } from '@hermes/shared'
import { atom } from 'nanostores'

import { withTimeout } from '@/lib/with-timeout'
import { markQuestionnaireDecided, QUESTIONNAIRE_DECIDE_DEADLINE_MS } from '@/store/onboarding-presence'

import { openQuestionnaire } from './store'

/** The questionnaire can run here (free tier on, local primary backend): Settings shows Run setup again. */
export const $questionnaireAvailable = atom(false)

export type OnboardingRequester = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

export function readRunState(value: unknown): null | OnboardingRunStateResult {
  if (typeof value !== 'object' || value === null) {
    return null
  }

  // SAFETY: a non-null object; both fields are type-checked below before use.
  const { eligible, run } = value as Partial<Record<keyof OnboardingRunStateResult, unknown>>

  return typeof run === 'boolean' && typeof eligible === 'boolean' ? { eligible, run } : null
}

async function readDue(request: OnboardingRequester): Promise<null | OnboardingRunStateResult> {
  try {
    return readRunState(
      await withTimeout(request<unknown>('onboarding.state'), QUESTIONNAIRE_DECIDE_DEADLINE_MS, 'onboarding.state timed out')
    )
  } catch {
    return null
  }
}

/** Due when the backend is the new shape and says both. Any failure is "not due". */
export async function questionnaireDue(request: OnboardingRequester): Promise<boolean> {
  const state = await readDue(request)

  return Boolean(state?.eligible && state.run)
}

/** Read the due check once per launch, open when due, and release everything waiting on the answer. */
export async function decideQuestionnaire(request: OnboardingRequester): Promise<boolean> {
  const state = await readDue(request)
  const due = Boolean(state?.eligible && state.run)

  $questionnaireAvailable.set(state?.eligible === true)

  if (due) {
    openQuestionnaire()
  }

  markQuestionnaireDecided()

  return due
}

/** Start and Skip write `false` (Start also marks the profile offer as made, D13); Run setup again writes `true`. */
export async function setRun(
  request: OnboardingRequester,
  run: boolean,
  { markProfileOffered = false }: { markProfileOffered?: boolean } = {}
): Promise<void> {
  await request('onboarding.set_run', { mark_profile_offered: markProfileOffered, run })
}

/** Settings → Run setup again: due on the next launch too, and open now without a reload. */
export async function runSetupAgain(request: OnboardingRequester): Promise<void> {
  await setRun(request, true)
  openQuestionnaire()
}
