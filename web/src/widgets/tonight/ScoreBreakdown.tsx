// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { CONDITION_FACTOR_LABEL, factorImpact, localizedFactorMeasurement, type ConditionFactor } from "./conditionFactors";
import { useLanguage } from "../../i18n/locale";

export function ScoreBreakdown({ factors }: { factors: ConditionFactor[] }) {
  const language = useLanguage();
  return (
    <section aria-label="Score breakdown" className="space-y-1.5">
      <h4 className="text-xs font-semibold text-fg">Score breakdown</h4>
      <p className="text-[11px] text-fg-muted">Starts at 100; listed deductions produce the final score.</p>
      <ul className="space-y-1.5 text-xs">
        {factors.map((factor) => (
          <li key={factor.name} className="grid grid-cols-[minmax(0,1fr)_auto] gap-x-2">
            <span className="font-medium text-fg">{CONDITION_FACTOR_LABEL[factor.name]}</span>
            <span className="text-right tabular-nums text-fg-muted">{factorImpact(factor)}</span>
            <span className="col-span-2 text-fg-muted">{localizedFactorMeasurement(factor, language)}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
