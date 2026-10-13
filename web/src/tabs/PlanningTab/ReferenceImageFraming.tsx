// SPDX-License-Identifier: GPL-3.0-only
// Copyright (C) 2026 Miroslav Bakoš
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { solveReferenceImage, type ReferenceImageSolution } from "../../api/referenceImage";

export function ReferenceImageFraming({ framingKey, onSolved }: {
  framingKey: string;
  onSolved: (raH: number, decDeg: number) => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [applied, setApplied] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const current = useRef({ framingKey, mounted: true });
  useLayoutEffect(() => { current.current.framingKey = framingKey; }, [framingKey]);
  useEffect(() => {
    const state = current.current;
    state.mounted = true;
    return () => { state.mounted = false; };
  }, []);
  const solve = useMutation({ mutationFn: solveReferenceImage });
  const apply = (solution: ReferenceImageSolution) => {
    onSolved(solution.ra_j2000_h, solution.dec_j2000_deg);
    setApplied(true);
  };
  const submit = async () => {
    if (file === null) return;
    const original = current.current.framingKey;
    setApplied(false);
    try {
      const solution = await solve.mutateAsync(file);
      if (current.current.mounted && current.current.framingKey === original) apply(solution);
    } catch { /* mutation owns the visible error */ }
  };
  return <Card>
    <label className="flex flex-col gap-2 text-sm">
      <span className="font-semibold">Frame from picture</span>
      <span className="text-xs text-fg-muted">Upload FITS, PNG or JPEG to move the blue FOV to its plate-solved position.</span>
      <input type="file" accept=".fits,.fit,.fts,.png,.jpg,.jpeg" disabled={solve.isPending}
        onChange={(event) => {
          const selected = event.currentTarget.files?.[0] ?? null;
          const tooLarge = selected !== null && selected.size > 64 * 1024 * 1024;
          setFile(tooLarge ? null : selected);
          setFileError(tooLarge ? "Reference image is larger than 64 MiB." : null);
          solve.reset(); setApplied(false);
        }} />
    </label>
    <div className="mt-2 flex flex-wrap items-center gap-2">
      <Button disabled={file === null || solve.isPending} onClick={() => { void submit(); }}>
        {solve.isPending ? "Solving picture…" : "Solve and place blue FOV"}
      </Button>
      {solve.data !== undefined && !applied &&
        <Button onClick={() => { apply(solve.data); }}>Apply solved position</Button>}
    </div>
    {solve.isPending && <p role="status" className="mt-2 text-xs text-fg-muted">Plate solving reference image</p>}
    {applied && <p role="status" className="mt-2 text-xs text-fg-muted">Solved position applied.</p>}
    {solve.data !== undefined && !applied &&
      <p role="status" className="mt-2 text-xs text-fg-muted">Framing changed while solving. Apply the solved position when ready.</p>}
    {fileError !== null && <p role="alert" className="mt-2 text-sm text-danger">{fileError}</p>}
    {solve.isError && <p role="alert" className="mt-2 text-sm text-danger">{solve.error.message}</p>}
  </Card>;
}
