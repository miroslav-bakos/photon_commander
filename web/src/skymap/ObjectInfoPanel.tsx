// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * M11's click-through info panel — what shows when a DSO glyph is clicked
 * (`onObjectClick`, wired by `SkyMap.tsx`). Floats over the map opposite the
 * zoom/layers column (`top-2 right-2` there; this is `bottom-10 left-2`, the
 * same "definite height between the credit bar and a fixed offset" trick
 * `LayersPanel.tsx`'s own doc comment explains), so it works the same way in
 * all three layouts without a fork (architecture policy's "one component tree").
 *
 * Renders all three of `GET /api/astro/objects/{designation}/info`'s honest
 * states (`objectInfoPanelState.ts`'s `resolveObjectInfoPanelState`) plus the
 * fetch's own loading/error states — never a blank panel and never a
 * spinner as an end state (docs/IMPLEMENTATION.md's M11 gate).
 *
 * Every credit comes from the response: `attributions` is rendered verbatim,
 * and the image's own `license_short_name`/`artist` are shown as the
 * response's own field values, never a hardcoded licence string
 * (architecture policy invariant 7, THIRD_PARTY_NOTICES.md's per-image finding).
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { objectInfoImageUrl, useAstroTargetQuery, useObjectInfoQuery, type ObjectInfoResponse } from "../api/astro";
import { MountSlewSyncControls } from "./MountSlewSyncControls";
import { resolveObjectInfoPanelState } from "./objectInfoPanelState";

export interface ObjectInfoPanelProps {
  designation: string;
  onClose: () => void;
  /** Make this object the Planning target. Optional: the panel is useful
   * read-only, and a host that has nowhere to put a target simply omits it
   * rather than being handed a button that does nothing. */
  onResolve?: (designation: string) => void;
}

export function ObjectInfoPanel({ designation, onClose, onResolve }: ObjectInfoPanelProps) {
  const query = useObjectInfoQuery(designation);
  // Slew/Sync need J2000 coordinates, which `GET /api/astro/objects/{id}/info`
  // (the query above, an encyclopedia lookup) does not carry — the same
  // `/api/astro/target` resolution `TargetPicker.tsx`'s own Resolve button
  // already uses. Fetched independently and gated on nothing beyond the
  // designation itself, since it costs nothing extra when unused (React
  // Query only requests it while this panel is mounted).
  const resolution = useAstroTargetQuery(designation);

  return (
    <div
      role="dialog"
      aria-label={`Object info: ${designation}`}
      className="absolute bottom-10 left-2 z-10 flex max-h-[70%] w-72 max-w-[calc(100%-1rem)] flex-col overflow-y-auto rounded-md border border-border-strong bg-surface/95 p-3 text-sm text-fg shadow-[0_1px_0_var(--color-shadow)] backdrop-blur-sm"
    >
      <div className="mb-2 flex items-start justify-between gap-2">
        <h3 className="min-w-0 truncate text-sm font-semibold text-fg">{designation}</h3>
        <button
          type="button"
          aria-label="Close object info"
          onClick={onClose}
          className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-md text-lg leading-none text-fg-muted hover:bg-surface-raised hover:text-fg"
        >
          ×
        </button>
      </div>

      {(onResolve !== undefined || resolution.data !== undefined) && (
        <div className="mb-3 flex min-w-0 flex-wrap items-start gap-2 border-b border-border pb-3">
          {onResolve !== undefined && (
            <button
              type="button"
              onClick={() => {
                onResolve(designation);
              }}
              className="min-h-11 flex-shrink-0 rounded-md border border-accent px-3 text-xs font-medium text-accent hover:bg-accent-muted"
            >
              Select target
            </button>
          )}
          {/* Beside "Select target" (DESIGN.md §3.5.1): the same button as
           * the search panel's, once the search panel imports it — see
           * `MountSlewSyncControls.tsx`'s own doc comment. Only rendered
           * once resolution has J2000 coordinates to slew/sync to; while
           * that lookup is pending or has failed, there is nothing to send
           * and no button is better than one with nowhere to go. */}
          {resolution.data !== undefined && (
            <MountSlewSyncControls
              className="min-w-0 flex-1"
              target={{
                label: designation,
                raHoursJ2000: resolution.data.ra_j2000_h,
                decDegJ2000: resolution.data.dec_j2000_deg,
              }}
            />
          )}
        </div>
      )}

      {/* A transient loading state is fine; it is never the state this
          panel settles on (docs/IMPLEMENTATION.md's M11 gate). */}
      {query.isPending && <p className="text-fg-muted">Loading…</p>}
      {query.isError && <p className="text-danger">{query.error.message}</p>}
      {query.data !== undefined && <ObjectInfoContent response={query.data} />}
    </div>
  );
}

function ObjectInfoContent({ response }: { response: ObjectInfoResponse }) {
  const state = resolveObjectInfoPanelState(response);

  if (state.kind === "not-configured") {
    return (
      <div className="flex flex-col gap-2">
        <p className="text-fg-muted">{state.message}</p>
        <p className="text-xs text-fg-muted">
          Turn this on in{" "}
          <Link to="/settings" className="text-link hover:underline">
            Settings → Services → Object info
          </Link>
          .
        </p>
      </div>
    );
  }

  if (state.kind === "not-found") {
    return <p className="text-fg-muted">{state.message}</p>;
  }

  return <FoundObjectInfo response={state.response} />;
}

function FoundObjectInfo({ response }: { response: ObjectInfoResponse }) {
  const [imageFailed, setImageFailed] = useState(false);
  const heading = response.title ?? response.common_name ?? response.designation;
  const commonName = response.common_name ?? null;
  const subheading = commonName !== null && commonName !== heading ? commonName : null;
  const image = response.image ?? null;
  const extract = response.extract ?? null;
  const instanceOf = response.instance_of ?? [];
  const discoveryDate = response.discovery_date ?? null;
  const articleUrl = response.article_url ?? null;
  const attributions = response.attributions ?? [];

  return (
    <div className="flex flex-col gap-3">
      <div>
        <p className="font-medium text-fg">{heading}</p>
        {subheading !== null && <p className="text-xs text-fg-muted">{subheading}</p>}
      </div>

      {image !== null && !imageFailed && (
        <figure className="flex flex-col gap-1">
          <img
            src={objectInfoImageUrl(response.designation)}
            alt={heading}
            className="w-full rounded-md border border-border object-cover"
            onError={() => {
              setImageFailed(true);
            }}
          />
          {/* The response's own field values, not a hardcoded licence
              string — a different file's edit could carry a different
              licence entirely (THIRD_PARTY_NOTICES.md's own per-image finding). */}
          <figcaption className="text-xs text-fg-muted">
            <a href={image.source_url} target="_blank" rel="noreferrer" className="text-link hover:underline">
              {image.license_short_name}
              {image.artist !== null && image.artist !== "" ? ` · ${image.artist}` : ""}
            </a>
          </figcaption>
        </figure>
      )}

      {extract !== null && <p className="text-fg-muted">{extract}</p>}

      {instanceOf.length > 0 && <p className="text-xs text-fg-muted">{instanceOf.join(", ")}</p>}

      {discoveryDate !== null && <p className="text-xs text-fg-muted">Discovered: {discoveryDate}</p>}

      {articleUrl !== null && (
        <a href={articleUrl} target="_blank" rel="noreferrer" className="text-xs text-link hover:underline">
          Read the full article on Wikipedia ↗
        </a>
      )}

      {attributions.length > 0 && (
        <ul className="flex flex-col gap-0.5 border-t border-border pt-2 text-xs text-fg-muted">
          {attributions.map((attribution) => (
            <li key={attribution.text}>
              {attribution.url !== null && attribution.url !== undefined ? (
                <a href={attribution.url} target="_blank" rel="noreferrer" className="text-link hover:underline">
                  {attribution.text}
                </a>
              ) : (
                attribution.text
              )}
            </li>
          ))}
        </ul>
      )}

      {response.cached && <p className="text-[10px] text-fg-disabled">Served from cache.</p>}
    </div>
  );
}
