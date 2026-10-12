// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { registerServiceWorker } from "./app/registerServiceWorker";
import "./index.css";

const rootElement = document.getElementById("root");
if (rootElement === null) {
  throw new Error("#root is missing from index.html");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// Production only — see registerServiceWorker.ts's own doc comment.
if (import.meta.env.PROD) {
  registerServiceWorker();
}
