import { atom } from 'nanostores'

import { writeJson } from '@/lib/storage'

export interface OnboardingAnswers {
  accent: null | string
}

const ANSWERS_KEY = 'hermes-onboarding-wizard-answers-v1'

export const DEFAULT_ANSWERS: OnboardingAnswers = { accent: null }

// Never read back: the theme boot deletes ANSWERS_KEY, and the accent lives in accentPref.
export const $onboardingAnswers = atom<OnboardingAnswers>(DEFAULT_ANSWERS)

export function setOnboardingAnswers(patch: Partial<OnboardingAnswers>): void {
  const next = { ...$onboardingAnswers.get(), ...patch }

  $onboardingAnswers.set(next)
  writeJson(ANSWERS_KEY, next)
}
