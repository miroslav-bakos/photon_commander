// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { createContext, createElement, memo, useContext, type ElementType, type Key, type ReactElement, type ReactNode } from "react";
import { Fragment as ReactFragment, jsx as reactJsx } from "react/jsx-runtime";
import { isTranslatable, translate, useLanguage } from "./locale";
export { Fragment } from "react/jsx-runtime";
export type { JSX } from "react/jsx-runtime";
const Enabled = createContext(true);
const textAttributes = ["title", "aria-label", "aria-description", "placeholder", "alt"] as const;
export const LocalizedText = memo(function LocalizedText({ value, children }: { value: string; children?: string }) {
  const locale = useLanguage();
  const enabled = useContext(Enabled);
  const text = children ?? value;
  return enabled ? translate(text, locale) : text;
});
function textChildren(value: ReactNode): ReactNode {
  if (typeof value === "string" && isTranslatable(value)) return reactJsx(LocalizedText, { value, children: value });
  if (Array.isArray(value)) return value.map((child: ReactNode, index: number) => {
    if (typeof child === "string" && isTranslatable(child)) return reactJsx(LocalizedText, { value: child, children: child }, `text:${String(index)}`);
    return Array.isArray(child) ? textChildren(child) : child;
  });
  return value;
}
/** A React-only presentation boundary, with no added DOM and no DOM rewriting.
 * Subscribing at text-bearing native elements refreshes labels without remounting
 * route components, resetting forms, or touching server state. Machine props
 * (value, href, id, data, events and refs) are always passed through unchanged. */
function LocalizedElement({ tag, original, children }: { tag: string; original: Record<string, unknown>; children?: ReactNode }) {
  const locale = useLanguage();
  const inherited = useContext(Enabled);
  const enabled = inherited && original.translate !== "no" && tag !== "code" && tag !== "pre";
  if (!enabled) return createElement(Enabled.Provider, { value: false }, createElement(tag, { ...original, children }));
  const props: Record<string, unknown> = { ...original, children };
  for (const attribute of textAttributes) {
    const value = props[attribute];
    if (typeof value === "string") props[attribute] = translate(value, locale);
  }
  return createElement(tag, props);
}
export function jsx(type: ElementType, props: unknown, key?: Key): ReactElement {
  if (props === null || typeof props !== "object") return reactJsx(type, props, key);
  if (type === ReactFragment) {
    const fragment = props as Record<string, unknown>;
    return reactJsx(type, { ...fragment, children: textChildren(fragment.children as ReactNode) }, key);
  }
  if (typeof type !== "string") return reactJsx(type, props, key);
  const original = props as Record<string, unknown>;
  const prepared = "children" in original ? { ...original, children: textChildren(original.children as ReactNode) } : original;
  if (type === "code" || type === "pre" || original.translate === "no" || textAttributes.some((attribute) => typeof original[attribute] === "string" && isTranslatable(original[attribute]))) {
    return reactJsx(LocalizedElement, { tag: type, original: prepared, children: prepared.children }, key);
  }
  return reactJsx(type, prepared, key);
}
export const jsxs = jsx;
