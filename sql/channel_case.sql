-- Channel mapping from a session's first source/medium (see docs/DATA_NOTES.md).
-- Kept in its own file, in portable SQL, so tests can run it in DuckDB.
-- Expects columns named `source` and `medium`.
CASE
  WHEN medium IS NULL
    OR medium IN ('<Other>', '(data deleted)', '(not set)', 'NULL', '') THEN 'unknown'
  WHEN LOWER(medium) = 'cpc' THEN 'paid_search'
  WHEN LOWER(medium) = 'organic' THEN 'organic_search'
  WHEN LOWER(medium) = 'referral' THEN 'referral'
  WHEN source = '(direct)' AND medium = '(none)' THEN 'direct'
  ELSE 'other'
END
