// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { useRef, type ReactNode } from "react";

interface Segment<T extends string> {
  value: T;
  label: string;
  icon?: ReactNode;
  disabled?: boolean;
  title?: string;
}

/** One joined selection control, rather than a row of unrelated actions.
 * Radio choices use roving focus. Action-backed pressed buttons and radios
 * inside a toolbar move focus with arrows but commit only on activation. */
export function SegmentedControl<T extends string>({ label, value, options, onChange, disabled = false, className = "", semantics = "radio" }: {
  label: string;
  value: T;
  options: readonly Segment<T>[];
  onChange: (value: T) => void;
  disabled?: boolean;
  className?: string;
  semantics?: "radio" | "pressed";
}) {
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const enabled = options.map((option, index) => !disabled && !option.disabled ? index : -1).filter((index) => index >= 0);
  const selected = options.findIndex((option) => option.value === value);
  const entry = enabled.includes(selected) ? selected : enabled[0];
  return (
    <div className={`ssc-segmented ${options.every((option) => option.icon) ? "ssc-segmented-icons" : "ssc-segmented-text"} ${className}`} role={semantics === "radio" ? "radiogroup" : "group"} aria-label={label} aria-disabled={disabled || undefined}>
      {options.map((option, index) => (
        <button key={option.value} ref={(element) => { buttons.current[index] = element; }} type="button"
          role={semantics === "radio" ? "radio" : undefined}
          aria-checked={semantics === "radio" ? value === option.value : undefined}
          aria-pressed={semantics === "pressed" ? value === option.value : undefined}
          aria-label={option.icon ? option.label : undefined}
          tabIndex={index === entry ? 0 : -1}
          disabled={disabled || option.disabled} title={option.title}
          className="ssc-segment"
          onClick={() => { if (semantics === "pressed" || value !== option.value) onChange(option.value); }}
          onKeyDown={(event) => {
            const position = enabled.indexOf(index);
            let next: number | undefined;
            const rtl = getComputedStyle(event.currentTarget).direction === "rtl";
            if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
              const direction = (event.key === "ArrowRight" ? 1 : -1) * (rtl ? -1 : 1);
              next = enabled[(position + direction + enabled.length) % enabled.length];
            } else if (event.key === "Home") next = enabled[0];
            else if (event.key === "End") next = enabled[enabled.length - 1];
            else return;
            event.preventDefault();
            if (next === undefined) return;
            buttons.current[next]?.focus();
            const choice = options[next];
            if (semantics === "radio" && !event.currentTarget.closest('[role="toolbar"]') && choice && choice.value !== value) onChange(choice.value);
          }}
        >{option.icon ?? option.label}</button>
      ))}
    </div>
  );
}
