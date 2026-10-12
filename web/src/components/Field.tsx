// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useId, type InputHTMLAttributes, type ReactNode } from "react";

interface FieldProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Classes for the wrapping <div>, not the input.
   *
   * `className` goes to the <input>. That is usually what a caller wants, but
   * it means layout classes like `flex-1` land on the wrong element: the
   * wrapper is the flex item, so the input grew while the wrapper stayed at its
   * intrinsic ~20ch width and refused to shrink, pushing the neighbouring
   * button out of its card. Layout belongs here; appearance belongs in
   * `className`. */
  wrapperClassName?: string;
  labelClassName?: string;
  label: string;
  hint?: ReactNode;
}

/** A labelled input — every control in this app is reachable by keyboard and
 * announced by a screen reader, which starts with `<label htmlFor>` actually
 * pointing at the input rather than merely wrapping it. */
export function Field({
  label,
  hint,
  id,
  className = "",
  wrapperClassName = "",
  labelClassName = "",
  "aria-describedby": describedBy,
  ...rest
}: FieldProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  const hintId = `${autoId}-hint`;
  const descriptionIds = [describedBy, hint !== undefined ? hintId : undefined].filter(Boolean).join(" ") || undefined;
  return (
    <div className={`ssc-field flex flex-col gap-1.5 ${wrapperClassName}`}>
      <label htmlFor={inputId} className={`ssc-field-label text-sm font-medium text-fg-muted ${labelClassName}`}>
        {label}
      </label>
      <input
        id={inputId}
        aria-describedby={descriptionIds}
        className={`ssc-field-input min-h-[44px] rounded-md border border-border-strong bg-bg px-3 text-base text-fg placeholder:text-fg-disabled lg:min-h-11 ${className}`}
        {...rest}
      />
      {hint !== undefined && <p id={hintId} className="ssc-field-hint text-xs text-fg-muted">{hint}</p>}
    </div>
  );
}
