-- Tag a captured frame with the scheduled run/block it belongs to, so
-- ``CapturePort.frames_captured`` (``ssc.scheduler.executor``, DESIGN.md
-- 4.3.0) can count committed catalogue rows scoped to one exposure block
-- rather than the whole table.
--
-- Both columns are nullable: a manual capture (`POST /api/capture/exposure`)
-- has no run or block behind it and leaves them NULL, exactly like `object`/
-- `filter` already do for an untargeted capture. Set once, at `begin_frame`
-- time — the same durable-before-acted-on insert CLAUDE.md invariant 6
-- already requires — never backfilled or reassigned afterwards.

ALTER TABLE capture_frame ADD COLUMN run_id TEXT;
ALTER TABLE capture_frame ADD COLUMN block_id TEXT;

-- What ``frames_captured`` actually queries: how many *complete* rows exist
-- for this run's block. A resumed process asks this before shooting its next
-- frame, so it has to be fast.
CREATE INDEX capture_frame_run_block ON capture_frame (run_id, block_id, status);
