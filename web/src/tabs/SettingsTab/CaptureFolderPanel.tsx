// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

import { useState } from "react";
import { Button } from "../../components/Button";
import { Card, CardTitle } from "../../components/Card";
import { Field } from "../../components/Field";
import { HostPathPicker } from "../../components/HostPathPicker";
import {
  useCalibrationPathTemplatesQuery,
  useCaptureFilenameTemplateQuery,
  useCaptureFolderSettingsQuery,
  useCaptureFolderTemplateQuery,
  useCapturePathTagsQuery,
  useCaptureRejectedFramesSettingsQuery,
  useSetCaptureFilenameTemplateMutation,
  useSetCalibrationPathTemplatesMutation,
  useSetCaptureFolderMutation,
  useSetCaptureFolderTemplateMutation,
  useSetCaptureRejectedFramesMutation,
  type CapturePathTag,
  type CalibrationPathTemplates,
} from "../../api/settings";

/**
 * Where captured FITS frames land, and how they are laid out underneath the
 * root folder (DESIGN.md §9.5.2). Three independent settings, saved
 * independently, matching `ssc/api/schemas.py`'s own split (confirmed on
 * disk, see `api/settings.ts`'s header for the full cross-check): the root
 * folder is `CaptureFolderRequest`/`Response`, the template is its own
 * `CaptureFolderTemplateRequest`/`Response` — not one combined body.
 *
 * The tag vocabulary is **fetched, never hardcoded** —
 * `useCapturePathTagsQuery` reads it from `GET /api/settings/capture/path-tags`,
 * generated from the backend's own `FrameMetadata` precisely so this panel
 * cannot go stale when a header field is added there.
 */
export function CaptureFolderPanel() {
  const folderSettings = useCaptureFolderSettingsQuery();
  const templateSettings = useCaptureFolderTemplateQuery();
  const filenameTemplateSettings = useCaptureFilenameTemplateQuery();
  const calibrationTemplates = useCalibrationPathTemplatesQuery();
  const pathTags = useCapturePathTagsQuery();
  const rejectedFrames = useCaptureRejectedFramesSettingsQuery();
  const saveFolder = useSetCaptureFolderMutation();
  const saveTemplate = useSetCaptureFolderTemplateMutation();
  const saveFilenameTemplate = useSetCaptureFilenameTemplateMutation();
  const saveCalibrationTemplates = useSetCalibrationPathTemplatesMutation();
  const saveRejectedFrames = useSetCaptureRejectedFramesMutation();

  const serverFolder = folderSettings.data?.folder ?? "";
  const serverTemplate = templateSettings.data?.template ?? "";
  const serverFilenameTemplate = filenameTemplateSettings.data?.template ?? "";
  const serverCalibrationTemplates = calibrationTemplates.data;
  const serverCalibrationKey = serverCalibrationTemplates === undefined
    ? ""
    : JSON.stringify(serverCalibrationTemplates);

  // Initialising from the query value matters on remount: TanStack can
  // already have server data cached when the user leaves Settings and comes
  // back. Starting these at "" would then make `seen* === server*` below and
  // leave all three inputs blank despite valid cached data.
  const [folderDraft, setFolderDraft] = useState(serverFolder);
  const [templateDraft, setTemplateDraft] = useState(serverTemplate);
  const [filenameTemplateDraft, setFilenameTemplateDraft] = useState(serverFilenameTemplate);
  const [biasFolderDraft, setBiasFolderDraft] = useState(
    serverCalibrationTemplates?.bias_folder_template ?? "",
  );
  const [darkFolderDraft, setDarkFolderDraft] = useState(
    serverCalibrationTemplates?.dark_folder_template ?? "",
  );
  const [flatFolderDraft, setFlatFolderDraft] = useState(
    serverCalibrationTemplates?.flat_folder_template ?? "",
  );
  const [calibrationFilenameDraft, setCalibrationFilenameDraft] = useState(
    serverCalibrationTemplates?.filename_template ?? "",
  );

  // Re-seed pristine drafts when the server value changes; preserve local edits.
  // (React's documented adjust-state-during-render pattern, not an effect —
  // `WeatherPanel.tsx` uses the identical technique) — a background refetch
  // landing between keystrokes must not wipe out what is being typed.
  const [seenFolder, setSeenFolder] = useState(serverFolder);
  if (seenFolder !== serverFolder) {
    setSeenFolder(serverFolder);
    if (folderDraft === seenFolder) setFolderDraft(serverFolder);
  }
  const [seenTemplate, setSeenTemplate] = useState(serverTemplate);
  if (seenTemplate !== serverTemplate) {
    setSeenTemplate(serverTemplate);
    if (templateDraft === seenTemplate) setTemplateDraft(serverTemplate);
  }
  const [seenFilenameTemplate, setSeenFilenameTemplate] = useState(serverFilenameTemplate);
  if (seenFilenameTemplate !== serverFilenameTemplate) {
    setSeenFilenameTemplate(serverFilenameTemplate);
    if (filenameTemplateDraft === seenFilenameTemplate) setFilenameTemplateDraft(serverFilenameTemplate);
  }
  const [seenCalibrationKey, setSeenCalibrationKey] = useState(serverCalibrationKey);
  if (seenCalibrationKey !== serverCalibrationKey && serverCalibrationTemplates !== undefined) {
    const previous = seenCalibrationKey === "" ? undefined : JSON.parse(seenCalibrationKey) as CalibrationPathTemplates;
    setSeenCalibrationKey(serverCalibrationKey);
    if (biasFolderDraft === (previous?.bias_folder_template ?? "")) setBiasFolderDraft(serverCalibrationTemplates.bias_folder_template);
    if (darkFolderDraft === (previous?.dark_folder_template ?? "")) setDarkFolderDraft(serverCalibrationTemplates.dark_folder_template);
    if (flatFolderDraft === (previous?.flat_folder_template ?? "")) setFlatFolderDraft(serverCalibrationTemplates.flat_folder_template);
    if (calibrationFilenameDraft === (previous?.filename_template ?? "")) setCalibrationFilenameDraft(serverCalibrationTemplates.filename_template);
  }

  if (
    folderSettings.isPending ||
    templateSettings.isPending ||
    filenameTemplateSettings.isPending ||
    calibrationTemplates.isPending ||
    rejectedFrames.isPending ||
    pathTags.isPending
  ) {
    return <Card>Loading capture folder settings…</Card>;
  }
  if (folderSettings.isError && folderSettings.data === undefined) {
    return (
      <Card className="text-danger">Could not load the capture folder: {folderSettings.error.message}</Card>
    );
  }
  if (templateSettings.isError && templateSettings.data === undefined) {
    return (
      <Card className="text-danger">
        Could not load the capture folder template: {templateSettings.error.message}
      </Card>
    );
  }
  if (filenameTemplateSettings.isError && filenameTemplateSettings.data === undefined) {
    return (
      <Card className="text-danger">
        Could not load the capture filename template: {filenameTemplateSettings.error.message}
      </Card>
    );
  }
  if (calibrationTemplates.isError && calibrationTemplates.data === undefined) {
    return (
      <Card className="text-danger">
        Could not load calibration path templates: {calibrationTemplates.error.message}
      </Card>
    );
  }
  if (pathTags.isError && pathTags.data === undefined) {
    return <Card className="text-danger">Could not load the path-tag vocabulary: {pathTags.error.message}</Card>;
  }
  if (rejectedFrames.isError && rejectedFrames.data === undefined) {
    return <Card className="text-danger">Could not load rejected-frame settings: {rejectedFrames.error.message}</Card>;
  }

  const tags = pathTags.data.tags;
  const defaultRoot = folderSettings.data.default_root;

  return (
    <Card>
      {(folderSettings.isError || templateSettings.isError || filenameTemplateSettings.isError || calibrationTemplates.isError || pathTags.isError || rejectedFrames.isError) && <p role="alert" className="mb-3 text-sm text-danger">Could not refresh settings. Showing the last saved values; your edits are retained.</p>}
      <CardTitle>Capture folder</CardTitle>
      <p className="mb-3 text-sm text-fg-muted">
        Where Photon Commander writes captured FITS frames, and how they are organised underneath that root.
        A capture refuses (rather than guessing) until a folder is set.
      </p>

      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-end gap-2">
          <Field
            label="Root folder"
            wrapperClassName="min-w-[16rem] flex-1"
            type="text"
            autoComplete="off"
            placeholder={defaultRoot ?? undefined}
            value={folderDraft}
            onChange={(e) => {
              setFolderDraft(e.target.value);
            }}
            hint="An absolute path on the machine running ssc-core, not this browser."
          />
          <Button
            variant="primary"
            aria-label="Save root folder"
            disabled={folderDraft === serverFolder || saveFolder.isPending}
            onClick={() => {
              saveFolder.mutate(folderDraft.trim() === "" ? null : folderDraft.trim());
            }}
          >
            {saveFolder.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
        <HostPathPicker label="capture folder" initialPath={folderDraft} directory onSelect={setFolderDraft} />
        {/* `default_root` is a suggestion only — never applied on its own
            (CLAUDE.md invariant 11's storage sibling rule, the response
            field's own docstring). Filling the draft still requires Save. */}
        {defaultRoot !== null && folderDraft.trim() === "" && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
            <span>
              Suggested: <span className="text-fg">{defaultRoot}</span>
            </span>
            <Button
              variant="secondary"
              onClick={() => {
                setFolderDraft(defaultRoot);
              }}
            >
              Use this
            </Button>
          </div>
        )}
        {saveFolder.isError && (
          <p role="alert" className="text-sm text-danger">
            {saveFolder.error.message}
          </p>
        )}
      </div>

      <div className="mt-4 flex flex-col gap-2 border-t border-border pt-3">
        <label className="flex min-h-11 items-center gap-3 text-sm text-fg">
          <input
            type="checkbox"
            checked={rejectedFrames.data.keep_rejected_frames}
            disabled={saveRejectedFrames.isPending}
            onChange={(event) => {
              saveRejectedFrames.mutate({ keep_rejected_frames: event.target.checked });
            }}
          />
          <span>
            Keep rejected frames
            <span className="block text-xs text-fg-muted">
              Move cloud-degraded lights to a rejected subfolder for inspection. Turn off to delete them.
            </span>
          </span>
        </label>
        {saveRejectedFrames.isError && (
          <p role="alert" className="text-sm text-danger">{saveRejectedFrames.error.message}</p>
        )}
      </div>

      <div className="mt-4 flex flex-col gap-2 border-t border-border pt-3">
        <div className="flex flex-wrap items-end gap-2">
          <Field
            label="Path template"
            wrapperClassName="min-w-[16rem] flex-1"
            type="text"
            autoComplete="off"
            value={templateDraft}
            onChange={(e) => {
              setTemplateDraft(e.target.value);
            }}
            hint="Relative to the root folder. Built from the tags listed below."
          />
          <Button
            variant="primary"
            aria-label="Save path template"
            disabled={templateDraft === serverTemplate || saveTemplate.isPending}
            onClick={() => {
              saveTemplate.mutate(templateDraft.trim());
            }}
          >
            {saveTemplate.isPending ? "Saving…" : "Save"}
          </Button>
        </div>

        <p className="text-xs text-fg-muted">
          Example: <span className="text-fg">{renderTemplateExample(templateDraft, tags)}</span>
        </p>

        {/* Shown verbatim: the server names the problem (unknown tag, `..`
            escape, absolute path) better than a generic message. */}
        {saveTemplate.isError && (
          <p role="alert" className="text-sm text-danger">
            {saveTemplate.error.message}
          </p>
        )}
      </div>

      <div className="mt-4 flex flex-col gap-2 border-t border-border pt-3">
        <div className="flex flex-wrap items-end gap-2">
          <Field
            label="Filename template"
            wrapperClassName="min-w-[16rem] flex-1"
            type="text"
            autoComplete="off"
            value={filenameTemplateDraft}
            onChange={(e) => {
              setFilenameTemplateDraft(e.target.value);
            }}
            hint="A filename stem built from the same tags. Photon Commander appends .fits automatically."
          />
          <Button
            variant="primary"
            aria-label="Save filename template"
            disabled={
              filenameTemplateDraft === serverFilenameTemplate || saveFilenameTemplate.isPending
            }
            onClick={() => {
              saveFilenameTemplate.mutate(filenameTemplateDraft.trim());
            }}
          >
            {saveFilenameTemplate.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
        <p className="text-xs text-fg-muted">
          Example: <span className="text-fg">{renderTemplateExample(filenameTemplateDraft, tags)}.fits</span>
        </p>
        {saveFilenameTemplate.isError && (
          <p role="alert" className="text-sm text-danger">
            {saveFilenameTemplate.error.message}
          </p>
        )}
      </div>

      <div className="mt-4 border-t border-border pt-3">
        <p className="mb-2 text-xs font-semibold tracking-wide text-fg-muted uppercase">
          Calibration frames
        </p>
        <div className="grid gap-2 md:grid-cols-2">
          <Field
            label="Bias folder"
            type="text"
            autoComplete="off"
            value={biasFolderDraft}
            onChange={(event) => { setBiasFolderDraft(event.target.value); }}
            hint="Relative to the root capture folder."
          />
          <Field
            label="Dark folder"
            type="text"
            autoComplete="off"
            value={darkFolderDraft}
            onChange={(event) => { setDarkFolderDraft(event.target.value); }}
            hint="Relative to the root capture folder."
          />
          <Field
            label="Flat folder"
            type="text"
            autoComplete="off"
            value={flatFolderDraft}
            onChange={(event) => { setFlatFolderDraft(event.target.value); }}
            hint="Use %FILTER% to separate flat sets."
          />
          <Field
            label="Calibration filename"
            type="text"
            autoComplete="off"
            value={calibrationFilenameDraft}
            onChange={(event) => { setCalibrationFilenameDraft(event.target.value); }}
            hint="Filename stem; .fits is appended automatically."
          />
        </div>
        <div className="mt-2 flex items-center justify-between gap-2">
          <p className="text-xs text-fg-muted">
            Example: <span className="text-fg">
              {renderTemplateExample(darkFolderDraft, tags)}/{renderTemplateExample(calibrationFilenameDraft, tags)}.fits
            </span>
          </p>
          <Button
            variant="primary"
            aria-label="Save calibration paths"
            disabled={
              saveCalibrationTemplates.isPending ||
              serverCalibrationTemplates === undefined ||
              (biasFolderDraft === serverCalibrationTemplates.bias_folder_template &&
                darkFolderDraft === serverCalibrationTemplates.dark_folder_template &&
                flatFolderDraft === serverCalibrationTemplates.flat_folder_template &&
                calibrationFilenameDraft === serverCalibrationTemplates.filename_template)
            }
            onClick={() => {
              saveCalibrationTemplates.mutate({
                bias_folder_template: biasFolderDraft.trim(),
                dark_folder_template: darkFolderDraft.trim(),
                flat_folder_template: flatFolderDraft.trim(),
                filename_template: calibrationFilenameDraft.trim(),
              });
            }}
          >
            {saveCalibrationTemplates.isPending ? "Saving…" : "Save"}
          </Button>
        </div>
        {saveCalibrationTemplates.isError && (
          <p role="alert" className="mt-2 text-sm text-danger">
            {saveCalibrationTemplates.error.message}
          </p>
        )}
      </div>

      <div className="mt-4 border-t border-border pt-3">
        <p className="mb-2 text-xs font-semibold tracking-wide text-fg-muted uppercase">Available tags</p>
        <ul className="space-y-1.5 text-sm">
          {tags.map((tag) => (
            <li key={tag.name} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
              <span className="font-mono text-fg">%{tag.name}%</span>
              <span className="text-fg-muted">{tag.description}</span>
              <span className="text-xs text-fg-disabled">e.g. {tag.example}</span>
            </li>
          ))}
        </ul>
      </div>
    </Card>
  );
}

/**
 * A pure string substitution, deliberately not schema-aware: each tag's
 * `%name%` token (`name` is the bare form the vocabulary reports, e.g.
 * `"TARGET"` — see `api/settings.ts`'s header) is replaced with its
 * `example` value wherever it appears in `template`. A token the vocabulary
 * does not know about is left as-is in the preview — the server is what
 * actually validates the template at save time, and rejects it verbatim;
 * this preview only ever shows what a valid template would produce.
 */
function renderTemplateExample(template: string, tags: CapturePathTag[]): string {
  let rendered = template;
  for (const tag of tags) {
    rendered = rendered.split(`%${tag.name}%`).join(tag.example);
  }
  return rendered;
}
