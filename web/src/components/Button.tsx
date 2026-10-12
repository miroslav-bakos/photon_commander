// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import type { ButtonHTMLAttributes, ReactNode } from "react";

type Variant = "primary" | "secondary" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  children: ReactNode;
}

const VARIANT_CLASSES: Record<Variant, string> = {
  primary: "bg-accent text-accent-fg hover:brightness-110",
  secondary: "bg-surface-raised text-fg border border-border-strong hover:bg-surface",
  danger: "bg-surface-raised text-danger border border-danger hover:bg-surface",
};

/** Every SSC button is >=44px tall (frontend-react.md: a gloved thumb in the
 * dark) and uses `type="button"` unless told otherwise, so a stray Enter in a
 * form field never submits an action that moves the mount. */
export function Button({ variant = "secondary", className = "", type = "button", ...props }: ButtonProps) {
  return (
    <button
      type={type}
      className={`ssc-button ssc-button-${variant} inline-flex min-h-[44px] items-center justify-center gap-2 rounded-full px-4 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 lg:min-h-11 ${VARIANT_CLASSES[variant]} ${className}`}
      {...props}
    />
  );
}
