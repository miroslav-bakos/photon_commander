-- Frame classification: light, dark, flat or bias (CLAUDE.md's capture
-- contract change, 2026-09-10 — "POST /api/capture/exposure ... replace
-- light: bool with frame_type", ssc.capture.frame.FrameType). A dark and a
-- flat were both indistinguishable as `light=false`; the folder template's
-- %IMAGETYPE% tag and the catalogue both need to tell them apart, durably.
--
-- Every row written before this migration was captured under the old
-- `light: bool` contract, which only ever commanded light frames in
-- practice (no dark/flat/bias path existed yet) — 'light' is not a guess
-- for that backfill, it is what every existing row actually is.

ALTER TABLE capture_frame ADD COLUMN frame_type TEXT NOT NULL
    CHECK (frame_type IN ('light', 'dark', 'flat', 'bias')) DEFAULT 'light';

CREATE INDEX capture_frame_type ON capture_frame (frame_type);
