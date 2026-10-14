ALTER TABLE capture_frame ADD COLUMN optic_role TEXT NOT NULL DEFAULT 'tele' CHECK(optic_role IN ('tele','wide'));
ALTER TABLE capture_frame ADD COLUMN frame_index INTEGER;
CREATE UNIQUE INDEX capture_frame_pair_member ON capture_frame(run_id, block_id, frame_index, optic_role) WHERE frame_index IS NOT NULL;
CREATE TABLE capture_run_companion (run_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)));
CREATE TABLE dual_capture_intent (
    run_id TEXT NOT NULL, block_id TEXT NOT NULL, frame_index INTEGER NOT NULL,
    intent TEXT NOT NULL,
    tele_issued INTEGER NOT NULL DEFAULT 0,
    wide_issued INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(run_id, block_id, frame_index)
);
