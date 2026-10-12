// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš

/**
 * Pins the brief's own verification requirements:
 * - the tag list renders from the fetched vocabulary, not a literal — a
 *   mocked vocabulary with an unexpected extra tag still renders it, which a
 *   hardcoded list would fail;
 * - the template's rendered example updates from the fetched tags' own
 *   example values as the user types;
 * - a save-time template rejection is displayed verbatim;
 * - `default_root` is shown as a suggestion only, never applied to the
 *   field on its own (architecture policy invariant 11's storage sibling rule).
 *
 * None of `GET`/`PUT /api/settings/capture-folder-template`,
 * `GET /api/settings/capture/path-tags`, or `CaptureFolderResponse.default_root`
 * are wired into `ssc/api/routes/settings.py` in this working copy yet
 * (`api/settings.ts`'s own header) — this file exercises the panel against
 * the schema classes that *have* landed on disk (`ssc/api/schemas.py`), not
 * a live backend, and the template endpoint's exact path is this file's own
 * inference (flagged there, not silently assumed).
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CalibrationPathTemplates, CapturePathTag } from "../../api/settings";
import { renderWithQueryClient } from "../../test/queryClient";
import { CaptureFolderPanel } from "./CaptureFolderPanel";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function json(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

const DEFAULT_TAGS: CapturePathTag[] = [
  { name: "TARGET", description: "The resolved target name", example: "M31" },
  { name: "DATEOFCAPTURE", description: "The capture date, local", example: "2026-09-10" },
  { name: "DATE", description: "The exposure start date, UTC", example: "20260910" },
  { name: "DATETIME", description: "The exposure start time, UTC", example: "20260910-220000" },
  { name: "IMAGETYPE", description: "lights, darks, flats or biases", example: "lights" },
  { name: "EXPOSURE", description: "The exposure duration", example: "30s" },
  { name: "CCDTEMP", description: "The sensor temperature", example: "-10.123" },
  { name: "FILTER", description: "The filter name", example: "LP" },
  { name: "SEQUENCE", description: "The exposure counter", example: "0003" },
];

function stubFetch(
  options: {
    folder?: string | null;
    default_root?: string | null;
    template?: string;
    filenameTemplate?: string;
    calibrationTemplates?: CalibrationPathTemplates;
    keepRejectedFrames?: boolean;
    tags?: CapturePathTag[];
    templatePutResponse?: () => Response;
    filenameTemplatePutResponse?: () => Response;
  } = {},
) {
  const folderPuts: unknown[] = [];
  const templatePuts: unknown[] = [];
  const filenameTemplatePuts: unknown[] = [];
  const rejectedFramePuts: unknown[] = [];
  const calibrationTemplatePuts: unknown[] = [];
  let folder = options.folder ?? null;
  let template = options.template ?? "%TARGET%/%DATEOFCAPTURE%/%IMAGETYPE%";
  let filenameTemplate =
    options.filenameTemplate ?? "%IMAGETYPE%_%TARGET%_%EXPOSURE%_%CCDTEMP%_%FILTER%_%DATETIME%_%SEQUENCE%";
  let keepRejectedFrames = options.keepRejectedFrames ?? true;
  let calibrationTemplates = options.calibrationTemplates ?? {
    bias_folder_template: "Calibration/bias/singles",
    dark_folder_template: "Calibration/darks/singles",
    flat_folder_template: "Calibration/flats/%FILTER%",
    filename_template: filenameTemplate,
  };
  const defaultRoot = options.default_root ?? null;
  const tags = options.tags ?? DEFAULT_TAGS;

  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(async (request: string | Request, requestInit?: RequestInit) => {
      const input = typeof request === "string" ? request : request.url;
      const init = typeof request === "string" ? requestInit : { method: request.method, body: await request.clone().text() };
      const { pathname } = new URL(input, "http://localhost");
      const method = init?.method ?? "GET";

      if (method === "GET" && pathname === "/api/settings/capture-folder") {
        return json({ folder, default_root: defaultRoot }, 200);
      }
      if (method === "PUT" && pathname === "/api/settings/capture-folder") {
        const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as { folder: string | null };
        folderPuts.push(body);
        folder = body.folder;
        return json({ folder, default_root: defaultRoot }, 200);
      }
      if (method === "GET" && pathname === "/api/settings/capture-folder-template") {
        return json({ template }, 200);
      }
      if (method === "PUT" && pathname === "/api/settings/capture-folder-template") {
        const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as { template: string };
        templatePuts.push(body);
        if (options.templatePutResponse !== undefined) return options.templatePutResponse();
        template = body.template;
        return json({ template }, 200);
      }
      if (method === "GET" && pathname === "/api/settings/capture-filename-template") {
        return json({ template: filenameTemplate }, 200);
      }
      if (method === "GET" && pathname === "/api/settings/capture-rejected-frames") {
        return json({ keep_rejected_frames: keepRejectedFrames }, 200);
      }
      if (method === "GET" && pathname === "/api/settings/calibration-path-templates") {
        return json(calibrationTemplates, 200);
      }
      if (method === "PUT" && pathname === "/api/settings/calibration-path-templates") {
        const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as CalibrationPathTemplates;
        calibrationTemplatePuts.push(body);
        calibrationTemplates = body;
        return json(body, 200);
      }
      if (method === "PUT" && pathname === "/api/settings/capture-rejected-frames") {
        const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as {
          keep_rejected_frames: boolean;
        };
        rejectedFramePuts.push(body);
        keepRejectedFrames = body.keep_rejected_frames;
        return json(body, 200);
      }
      if (method === "PUT" && pathname === "/api/settings/capture-filename-template") {
        const body = JSON.parse(typeof init?.body === "string" ? init.body : "{}") as {
          template: string;
        };
        filenameTemplatePuts.push(body);
        if (options.filenameTemplatePutResponse !== undefined) {
          return options.filenameTemplatePutResponse();
        }
        filenameTemplate = body.template;
        return json({ template: filenameTemplate }, 200);
      }
      if (method === "GET" && pathname === "/api/settings/capture/path-tags") {
        return json({ tags }, 200);
      }
      return json({ detail: `unexpected ${method} ${pathname}` }, 500);
    }),
  );
  return { calibrationTemplatePuts, filenameTemplatePuts, folderPuts, rejectedFramePuts, templatePuts };
}

describe("CaptureFolderPanel", () => {
  it("loads the root folder and path template from the server", async () => {
    stubFetch({ folder: "/data/ssc-frames", template: "%TARGET%/%IMAGETYPE%" });
    renderWithQueryClient(<CaptureFolderPanel />);

    expect(await screen.findByDisplayValue("/data/ssc-frames")).toBeInTheDocument();
    expect(screen.getByDisplayValue("%TARGET%/%IMAGETYPE%")).toBeInTheDocument();
    expect(screen.getByLabelText("Filename template")).toHaveValue(
      "%IMAGETYPE%_%TARGET%_%EXPOSURE%_%CCDTEMP%_%FILTER%_%DATETIME%_%SEQUENCE%",
    );
    expect(screen.getByDisplayValue("Calibration/darks/singles")).toBeInTheDocument();
  });

  it("saves calibration-specific folders and filename pattern together", async () => {
    const { calibrationTemplatePuts } = stubFetch();
    renderWithQueryClient(<CaptureFolderPanel />);

    fireEvent.change(await screen.findByLabelText("Dark folder"), {
      target: { value: "Calibration/%IMAGETYPE%/%EXPOSURE%" },
    });
    fireEvent.change(screen.getByLabelText("Calibration filename"), {
      target: { value: "%IMAGETYPE%-%GAIN%-%SEQUENCE%" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save calibration paths" }));

    await waitFor(() => {
      expect(calibrationTemplatePuts).toEqual([
        {
          bias_folder_template: "Calibration/bias/singles",
          dark_folder_template: "Calibration/%IMAGETYPE%/%EXPOSURE%",
          flat_folder_template: "Calibration/flats/%FILTER%",
          filename_template: "%IMAGETYPE%-%GAIN%-%SEQUENCE%",
        },
      ]);
    });
  });

  it("keeps rejected frames by default and can switch to deletion", async () => {
    const { rejectedFramePuts } = stubFetch();
    renderWithQueryClient(<CaptureFolderPanel />);

    const toggle = await screen.findByRole("checkbox", { name: /keep rejected frames/i });
    expect(toggle).toBeChecked();
    fireEvent.click(toggle);

    await waitFor(() => {
      expect(rejectedFramePuts).toEqual([{ keep_rejected_frames: false }]);
    });
  });

  it("repopulates all fields from cached server data after leaving and returning", async () => {
    stubFetch({
      folder: "/data/ssc-frames",
      template: "%TARGET%/%IMAGETYPE%",
      filenameTemplate: "%TARGET%-%DATETIME%",
    });
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: Infinity } },
    });
    const first = render(
      <QueryClientProvider client={client}>
        <CaptureFolderPanel />
      </QueryClientProvider>,
    );
    await screen.findByDisplayValue("/data/ssc-frames");
    first.unmount();

    render(
      <QueryClientProvider client={client}>
        <CaptureFolderPanel />
      </QueryClientProvider>,
    );
    expect(screen.getByLabelText("Root folder")).toHaveValue("/data/ssc-frames");
    expect(screen.getByLabelText("Path template")).toHaveValue("%TARGET%/%IMAGETYPE%");
    expect(screen.getByLabelText("Filename template")).toHaveValue("%TARGET%-%DATETIME%");
  });

  it("renders every fetched tag, including one no hardcoded list would know about", async () => {
    stubFetch({
      tags: [
        ...DEFAULT_TAGS,
        { name: "NEWFIELD", description: "Added to FrameMetadata after this panel was written", example: "xyz" },
      ],
    });
    renderWithQueryClient(<CaptureFolderPanel />);

    expect(await screen.findByText("%NEWFIELD%")).toBeInTheDocument();
    expect(screen.getByText("Added to FrameMetadata after this panel was written")).toBeInTheDocument();
    expect(screen.getByText("e.g. xyz")).toBeInTheDocument();
  });

  it("shows the rendered example from the fetched tags' example values, and updates it as the template is typed", async () => {
    stubFetch();
    renderWithQueryClient(<CaptureFolderPanel />);

    await screen.findByDisplayValue("%TARGET%/%DATEOFCAPTURE%/%IMAGETYPE%");
    expect(screen.getByText("M31/2026-09-10/lights")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Path template"), { target: { value: "%IMAGETYPE%/%TARGET%" } });
    expect(screen.getByText("lights/M31")).toBeInTheDocument();
  });

  it("lists the UTC date-only tag and previews it in paths and filenames", async () => {
    stubFetch();
    renderWithQueryClient(<CaptureFolderPanel />);

    expect(await screen.findByText("%DATE%")).toBeInTheDocument();
    expect(screen.getByText("The exposure start date, UTC")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Path template"), { target: { value: "%DATE%/%IMAGETYPE%" } });
    fireEvent.change(screen.getByLabelText("Filename template"), { target: { value: "%DATE%_%SEQUENCE%" } });

    expect(screen.getByText("20260910/lights")).toBeInTheDocument();
    expect(screen.getByText("20260910_0003.fits")).toBeInTheDocument();
  });

  it("leaves a token the vocabulary does not know about untouched in the preview", async () => {
    stubFetch();
    renderWithQueryClient(<CaptureFolderPanel />);
    await screen.findByLabelText("Path template");

    fireEvent.change(screen.getByLabelText("Path template"), { target: { value: "%TARGET%/%BOGUS%" } });
    expect(screen.getByText("M31/%BOGUS%")).toBeInTheDocument();
  });

  it("sends the trimmed folder on save, independently of the template", async () => {
    const { folderPuts, templatePuts } = stubFetch({ folder: null });
    renderWithQueryClient(<CaptureFolderPanel />);

    await screen.findByLabelText("Root folder");
    fireEvent.change(screen.getByLabelText("Root folder"), { target: { value: "/data/ssc-frames " } });
    fireEvent.click(screen.getByRole("button", { name: "Save root folder" }));

    await waitFor(() => {
      expect(folderPuts).toEqual([{ folder: "/data/ssc-frames" }]);
    });
    expect(templatePuts).toEqual([]);
  });

  it("sends the trimmed template on save, independently of the folder", async () => {
    const { folderPuts, templatePuts } = stubFetch();
    renderWithQueryClient(<CaptureFolderPanel />);

    await screen.findByLabelText("Path template");
    fireEvent.change(screen.getByLabelText("Path template"), { target: { value: " %TARGET% " } });
    fireEvent.click(screen.getByRole("button", { name: "Save path template" }));

    await waitFor(() => {
      expect(templatePuts).toEqual([{ template: "%TARGET%" }]);
    });
    expect(folderPuts).toEqual([]);
  });

  it("previews and saves the filename template independently", async () => {
    const { filenameTemplatePuts, folderPuts, templatePuts } = stubFetch();
    renderWithQueryClient(<CaptureFolderPanel />);

    await screen.findByLabelText("Filename template");
    expect(
      screen.getByText("lights_M31_30s_-10.123_LP_20260910-220000_0003.fits"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filename template"), {
      target: { value: " %TARGET%-%SEQUENCE% " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save filename template" }));

    await waitFor(() => {
      expect(filenameTemplatePuts).toEqual([{ template: "%TARGET%-%SEQUENCE%" }]);
    });
    expect(folderPuts).toEqual([]);
    expect(templatePuts).toEqual([]);
  });

  it("shows a save-time template rejection verbatim", async () => {
    stubFetch({
      templatePutResponse: () => json({ detail: "unknown tag %BOGUS% in path template" }, 422),
    });
    renderWithQueryClient(<CaptureFolderPanel />);

    await screen.findByLabelText("Path template");
    fireEvent.change(screen.getByLabelText("Path template"), { target: { value: "%BOGUS%" } });
    fireEvent.click(screen.getByRole("button", { name: "Save path template" }));

    expect(await screen.findByText("unknown tag %BOGUS% in path template")).toBeInTheDocument();
  });

  it("shows default_root as a suggestion, never applying it until asked", async () => {
    stubFetch({ folder: null, default_root: "/home/owner/Pictures/SSC" });
    renderWithQueryClient(<CaptureFolderPanel />);

    await screen.findByLabelText("Root folder");
    // Never prefilled into the field itself.
    expect(screen.getByLabelText("Root folder")).toHaveValue("");
    expect(screen.getByText("/home/owner/Pictures/SSC")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Use this" }));
    expect(screen.getByLabelText("Root folder")).toHaveValue("/home/owner/Pictures/SSC");
  });
});

it("keeps a newer root-folder draft after an acknowledged save with a hanging refresh", async () => {
  stubFetch({ folder: "/old" });
  const original = globalThis.fetch;
  let finish!: (response: Response) => void;
  let saved = false;
  vi.stubGlobal("fetch", vi.fn((input: Request) => {
    const path = new URL(input.url).pathname;
    if (path === "/api/settings/capture-folder" && input.method === "PUT") {
      return new Promise<Response>((resolve) => { finish = resolve; });
    }
    if (path === "/api/settings/capture-folder" && saved) return new Promise<Response>(() => { /* Keep the background refresh pending. */ });
    return original(input);
  }));
  renderWithQueryClient(<CaptureFolderPanel />);
  const folder = await screen.findByLabelText("Root folder");
  await waitFor(() => { expect(folder).toHaveValue("/old"); });
  fireEvent.change(folder, { target: { value: "/first" } });
  fireEvent.click(screen.getByRole("button", { name: "Save root folder" }));
  await waitFor(() => { expect(finish).toBeDefined(); });
  fireEvent.change(folder, { target: { value: "/newer" } });
  saved = true;
  await act(async () => { finish(json({ folder: "/first", default_root: null }, 200)); await Promise.resolve(); });
  await waitFor(() => { expect(screen.getByRole("button", { name: "Save root folder" })).toBeEnabled(); });
  expect(folder).toHaveValue("/newer");
});
