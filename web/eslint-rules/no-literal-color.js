// @ts-check
/**
 * Every colour in this app comes from a CSS variable defined once in
 * `src/theme/tokens.css` (dark and red-light). A literal colour anywhere in a
 * component is exactly the thing that is expensive to notice missing when
 * red-light mode is audited later — so it is a lint error, not a convention.
 *
 * Three things get flagged, all string- or template-literal based (no runtime
 * cost, no dependency):
 *
 *   1. A hex colour (#fff, #ffffff, #ffffffff) anywhere in a string.
 *   2. An `rgb()/rgba()/hsl()/hsla()/hwb()/lab()/lch()/oklab()/oklch()` call
 *      anywhere in a string.
 *   3. A Tailwind utility that names a colour directly — either the default
 *      palette (`bg-red-500`, `text-white`, `ring-blue-400`, ...) or an
 *      arbitrary-value named colour (`text-[white]`) — because both bypass the
 *      token system even though (1) and (2) would not catch them.
 *
 * `var(--...)`, `currentColor`, `transparent` and `inherit` are the escape
 * hatches, and none of them trip this rule.
 */

const HEX_COLOR = /#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\b/;
const COLOR_FUNCTION =
  /\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch|color)\(\s*[^)]*\)/i;

const TAILWIND_COLOR_NAMES = [
  "inherit",
  "current",
  "black",
  "white",
  "slate",
  "gray",
  "grey",
  "zinc",
  "neutral",
  "stone",
  "red",
  "orange",
  "amber",
  "yellow",
  "lime",
  "green",
  "emerald",
  "teal",
  "cyan",
  "sky",
  "blue",
  "indigo",
  "violet",
  "purple",
  "fuchsia",
  "pink",
  "rose",
];

const TAILWIND_PREFIXES = [
  "bg",
  "text",
  "border",
  "ring",
  "ring-offset",
  "outline",
  "fill",
  "stroke",
  "from",
  "via",
  "to",
  "shadow",
  "divide",
  "decoration",
  "caret",
  "accent",
  "placeholder",
];

const TAILWIND_PALETTE_UTILITY = new RegExp(
  `(?:^|[\\s"'\`])(?:${TAILWIND_PREFIXES.join("|")})-(?:${TAILWIND_COLOR_NAMES.join("|")})(?:-\\d{2,3})?(?=[\\s"'\`]|$)`,
);

const TAILWIND_ARBITRARY_NAMED = new RegExp(
  `(?:${TAILWIND_PREFIXES.join("|")})-\\[(?:${TAILWIND_COLOR_NAMES.join("|")})\\]`,
  "i",
);

/** @param {string} value */
function findViolation(value) {
  if (HEX_COLOR.test(value)) return "a hex colour literal";
  if (COLOR_FUNCTION.test(value)) return "a raw colour function call";
  if (TAILWIND_ARBITRARY_NAMED.test(value)) {
    return "a Tailwind arbitrary-value named colour";
  }
  if (TAILWIND_PALETTE_UTILITY.test(value)) {
    return "a default Tailwind palette colour utility";
  }
  return null;
}

/** @type {import('eslint').Rule.RuleModule} */
const rule = {
  meta: {
    type: "problem",
    docs: {
      description:
        "disallow literal colours; every colour must come from a CSS variable token",
    },
    schema: [],
    messages: {
      literalColor:
        "{{what}} is not allowed here — use a CSS variable token from src/theme/tokens.css (var(--...) or its Tailwind alias) so both themes stay correct.",
    },
  },
  create(context) {
    /** @param {import('estree').Literal} node */
    function checkLiteral(node) {
      if (typeof node.value !== "string") return;
      const what = findViolation(node.value);
      if (what) {
        context.report({ node, messageId: "literalColor", data: { what } });
      }
    }

    /** @param {import('estree').TemplateLiteral} node */
    function checkTemplateLiteral(node) {
      for (const quasi of node.quasis) {
        const what = findViolation(quasi.value.raw);
        if (what) {
          context.report({ node: quasi, messageId: "literalColor", data: { what } });
          return;
        }
      }
    }

    return {
      Literal: checkLiteral,
      TemplateLiteral: checkTemplateLiteral,
    };
  },
};

export default rule;
