import { useStore } from '@nanostores/react'

import { Button } from '@/components/ui/button'
import { useI18n } from '@/i18n'
import { Loader2 } from '@/lib/icons'

import { chosenName } from '../flow'
import { handoffPrompt } from '../handoff'
import { $questionnaire } from '../store'

/** The last screen: the first message as Hermes will get it, and Start. */
export function ReviewStep({ onStart }: { onStart: () => void }) {
  const { t } = useI18n()
  const { answers, facts, starting } = useStore($questionnaire)
  const copy = t.questionnaire.start
  const prompt = handoffPrompt(facts, answers)

  return (
    <div className="grid gap-4">
      <div className="grid gap-1">
        <h3 className="text-lg font-semibold tracking-tight">{copy.title(chosenName(answers))}</h3>
        <p className="text-sm text-muted-foreground">{copy.sub}</p>
      </div>
      <pre className="max-h-64 overflow-y-auto rounded-lg bg-muted px-4 py-3 font-sans text-[0.8125rem] leading-5 whitespace-pre-wrap text-foreground">
        {prompt}
      </pre>
      <div className="flex justify-end">
        <Button disabled={starting} onClick={onStart} type="button">
          {starting ? (
            <>
              <Loader2 className="animate-spin" />
              {copy.waiting}
            </>
          ) : (
            copy.action
          )}
        </Button>
      </div>
    </div>
  )
}
