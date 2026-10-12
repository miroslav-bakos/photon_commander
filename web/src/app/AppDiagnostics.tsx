// SPDX-License-Identifier: GPL-3.0-only
import { useEffect } from "react";
import { useLocation } from "react-router-dom";

const screens = new Set(["dashboard", "planning", "scheduling", "calendar", "mount", "polar-align", "live", "live-stacking", "streaming", "archive", "system", "settings", "help"]);
/** Never read a label, URL query, error message, input value or DOM content. */
export function AppDiagnostics() {
  const { pathname } = useLocation();
  useEffect(() => {
    const candidate = pathname.slice(1);
    const screen = screens.has(candidate) ? candidate : "other";
    let last = 0;
    function record(kind: "navigation" | "action" | "error" | "unhandledrejection", control = "none") {
      if (Date.now() - last < 500) return;
      last = Date.now();
      void fetch("/api/bug-reports/event", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, screen, control }),
      }).catch(() => { /* A failed diagnostic request must not create an error loop. */ });
    }
    function click(event: MouseEvent) {
      const control = event.target instanceof Element ? event.target.closest("button,a") : null;
      if (control) record("action", control.tagName === "A" ? "link" : "button");
    }
    const error = () => { record("error"); };
    const rejection = () => { record("unhandledrejection"); };
    record("navigation");
    document.addEventListener("click", click, true);
    window.addEventListener("error", error);
    window.addEventListener("unhandledrejection", rejection);
    return () => {
      document.removeEventListener("click", click, true);
      window.removeEventListener("error", error);
      window.removeEventListener("unhandledrejection", rejection);
    };
  }, [pathname]);
  return null;
}
