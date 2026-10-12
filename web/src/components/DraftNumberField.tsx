// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState, type ComponentProps } from "react";
import { Field } from "./Field";

type FieldProps = ComponentProps<typeof Field>;

/**
 * A numeric field whose text remains editable while focused.
 *
 * A controlled `<input type="number">` bound directly to a number cannot
 * represent the intermediate empty string used while replacing `30` with
 * `60`: the model immediately writes `30` (or a fallback digit) back into
 * the field.  This component owns that transient string, publishes valid
 * numbers as they are typed. Optional fields publish `null` when cleared,
 * while the local draft keeps the empty text visible for replacement.
 */
export function DraftNumberField({
  value,
  nullable = false,
  onValueChange,
  onInvalid,
  onFocus,
  ...props
}: Omit<FieldProps, "type" | "value" | "onChange" | "onBlur" | "onKeyDown"> & {
  value: number | null;
  nullable?: boolean;
  onValueChange: (value: number | null) => void;
  onInvalid?: () => void;
}) {
  const [text, setText] = useState("");
  const [focused, setFocused] = useState(false);
  const shownText = focused ? text : value === null ? "" : String(value);

  const commit = () => {
    const trimmed = text.trim();
    if (trimmed === "") {
      if (nullable) onValueChange(null);
      else setText(value === null ? "" : String(value));
      return;
    }
    const parsed = Number(trimmed);
    if (!Number.isFinite(parsed)) {
      setText(value === null ? "" : String(value));
      onInvalid?.();
      return;
    }
    onValueChange(parsed);
    setText(String(parsed));
  };

  return (
    <Field
      {...props}
      type="number"
      value={shownText}
      onFocus={(event) => {
        setText(value === null ? "" : String(value));
        setFocused(true);
        onFocus?.(event);
      }}
      onChange={(event) => {
        const raw = event.target.value;
        setText(raw);
        if (raw.trim() === "") {
          if (nullable) onValueChange(null);
          return;
        }
        const parsed = Number(raw);
        if (Number.isFinite(parsed)) onValueChange(parsed);
      }}
      onBlur={() => {
        commit();
        setFocused(false);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter") event.currentTarget.blur();
        if (event.key === "Escape") {
          setText(value === null ? "" : String(value));
          event.currentTarget.blur();
        }
      }}
    />
  );
}
