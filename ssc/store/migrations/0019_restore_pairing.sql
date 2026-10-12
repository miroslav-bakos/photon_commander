-- Restore pairing once for existing loginless installations. New settings
-- default to paired access; explicit environment overrides remain authoritative.
UPDATE config
SET value = json_set(value, '$.auth_required', json('true')),
    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
WHERE key = 'security';
