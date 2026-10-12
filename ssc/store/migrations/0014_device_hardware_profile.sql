-- Bind each remembered physical telescope address to its optical/hardware profile.
-- NULL is deliberate: an unknown model must be confirmed, never guessed.
ALTER TABLE device_address ADD COLUMN hardware_profile_id TEXT;
