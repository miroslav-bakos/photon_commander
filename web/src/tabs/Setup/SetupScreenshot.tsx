// SPDX-License-Identifier: GPL-3.0-only
/** Only verified manufacturer screenshots belong here. Coordinates are percentages
 * of the source image; annotations are also listed as text for screen readers. */
export interface SetupScreenshotAsset {
  src: string;
  alt: string;
  sourceUrl: string;
  sourceLabel: string;
  appVersion: string;
  callouts: { label: string; xPercent: number; yPercent: number }[];
}
export function SetupScreenshot({ asset }: { asset: SetupScreenshotAsset }) {
  return <figure className="space-y-2">
    <div className="relative mx-auto max-w-lg">
      <img src={asset.src} alt={asset.alt} className="h-auto w-full rounded-md border border-border" />
      {asset.callouts.map((callout, index) => <span key={`${String(index)}-${callout.label}`} aria-hidden="true" className="absolute flex h-7 w-7 items-center justify-center rounded-full border-2 border-fg bg-accent text-sm font-bold text-accent-fg shadow" style={{ left: `${String(callout.xPercent)}%`, top: `${String(callout.yPercent)}%`, transform: "translate(-50%, -50%)" }}>{index + 1}</span>)}
    </div>
    <figcaption className="text-sm text-fg-muted"><a href={asset.sourceUrl} target="_blank" rel="noreferrer" className="text-link underline">{asset.sourceLabel}</a> · {asset.appVersion}
      <ol className="list-decimal pl-5">{asset.callouts.map((callout, index) => <li key={`${String(index)}-${callout.label}`}>{callout.label}</li>)}</ol>
    </figcaption>
  </figure>;
}
