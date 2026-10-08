import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { $questionnaireDecided } from '@/store/onboarding-presence'

import { $questionnaireAvailable, decideQuestionnaire, type OnboardingRequester, readRunState } from './due'
import { $questionnaire, closeQuestionnaire } from './store'

const answering =
  (value: null | Record<string, boolean | number | string>): OnboardingRequester =>
  async <T>() =>
    // SAFETY: the due check validates the answer's shape before reading it.
    value as T

const failing: OnboardingRequester = async () => {
  throw Object.assign(new Error('method not found'), { code: -32601 })
}

beforeEach(() => $questionnaireDecided.set(false))

afterEach(() => closeQuestionnaire('skipped'))

describe('questionnaire due check', () => {
  it('opens when the backend says run and eligible, and releases the waiters', async () => {
    expect(await decideQuestionnaire(answering({ eligible: true, run: true }))).toBe(true)
    expect($questionnaire.get().phase).toBe('shown')
    expect($questionnaireDecided.get()).toBe(true)
    expect($questionnaireAvailable.get()).toBe(true)
  })

  it.each([
    ['not eligible', { eligible: false, run: true }],
    ['not due', { eligible: true, run: false }],
    ['the old shape under the same name', { eligible: true, failed_starts: 0, intro: 'unseen' }],
    ['a non-bool run', { eligible: true, run: 'yes' }],
    ['no answer', null]
  ])('is not due for %s', async (_case, value) => {
    expect(await decideQuestionnaire(answering(value))).toBe(false)
    expect($questionnaire.get().phase).not.toBe('shown')
    expect($questionnaireDecided.get()).toBe(true)
  })

  it('is not due when the backend lacks the method', async () => {
    expect(await decideQuestionnaire(failing)).toBe(false)
    expect($questionnaireDecided.get()).toBe(true)
    expect($questionnaireAvailable.get()).toBe(false)
  })

  it('reads only the new shape', () => {
    expect(readRunState({ eligible: true, run: false })).toEqual({ eligible: true, run: false })
    const agenticStateAnswer = { eligible: true, intro: 'seen' }

    expect(readRunState(agenticStateAnswer)).toBeNull()
  })
})
