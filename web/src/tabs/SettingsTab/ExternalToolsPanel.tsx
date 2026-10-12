// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { Link } from "react-router-dom";

import { useExternalToolsQuery, type ExternalTool } from "../../api/settings";
import { Badge } from "../../components/Badge";
import { ToolPathsPanel } from "./ToolPathsPanel";

function statusLabel(tool: ExternalTool): string {
  if (tool.status === "installed") return "Installed";
  if (tool.status === "incomplete") return "Needs setup";
  return "Not installed";
}

export function ExternalToolsPanel() {
  const catalog = useExternalToolsQuery();
  if (catalog.isPending) return <p className="text-sm text-fg-muted">Checking this computer…</p>;
  if (catalog.isError) return <p className="text-sm text-danger">Could not inspect optional tools: {catalog.error.message}</p>;

  return (
    <div className="space-y-3">
      <ToolPathsPanel />
      <p className="text-xs text-fg-muted">
        Host: {catalog.data.platform} · {catalog.data.architecture}. Tools remain separate from Photon Commander and keep their own licences.
      </p>
      {catalog.data.tools.map((tool) => (
        <article key={tool.id} className="rounded-md border border-border bg-surface-raised p-3">
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <h3 className="font-medium">{tool.name}</h3>
              <p className="mt-1 text-sm text-fg-muted">{tool.purpose}</p>
            </div>
            <Badge tone={tool.status === "installed" ? "success" : tool.status === "incomplete" ? "warning" : "neutral"}>
              {statusLabel(tool)}
            </Badge>
          </div>
          <p className="mt-2 text-sm text-fg-muted">{tool.detail}</p>
          <dl className="mt-2 grid gap-1 text-xs text-fg-muted sm:grid-cols-2">
            <div><dt className="inline font-medium text-fg">Licence: </dt><dd className="inline">{tool.license}</dd></div>
            <div><dt className="inline font-medium text-fg">Platforms: </dt><dd className="inline">{tool.supported_platforms.join(", ")}</dd></div>
          </dl>
          {tool.detected_path !== null && <p className="mt-2 break-all font-mono text-xs text-fg-muted">{tool.detected_path}</p>}
          <div className="mt-3 flex flex-wrap gap-3">
            <Link to={`/help?section=knowledge&article=${tool.guide_article_id}`} className="inline-flex min-h-11 items-center text-sm font-medium text-link underline">
              Setup guide
            </Link>
            <a href={tool.source_url} target="_blank" rel="noreferrer" className="inline-flex min-h-11 items-center text-sm font-medium text-link underline">
              {tool.id === "astap" || tool.id === "ffmpeg" ? "Download & install" : "Upstream source"}
            </a>
          </div>
        </article>
      ))}
    </div>
  );
}
