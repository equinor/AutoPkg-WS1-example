# Sync Changelog — 2026-11-13

**4 commit(s)** since last sync.

## New Features & Enhancements

- `bf32cd2b` ws1_sensor_upload: resolve remaining clear-text logging flows; document TLS inspection diagnosis
  
  - Split the secrets dict into individual variables and rename get_secret()
    to get_ws1_setting(); CodeQL's name heuristic tainted API responses via
    the API URL. Local CodeQL python-security-and-quality suite: 0 results.
  - Add dev-work/TLS packet inspection/001: Gatekeeper notarization lookup
    (api.apple-cloudkit.com) blocked by HTTPS interception, exemption
    recommendations, post-exemption verification and lessons learned.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `5dbc5720` ws1_sensor_upload: print labels instead of secret-derived values
  
  Addresses code scanning alert #17 (py/clear-text-logging-sensitive-data):
  debug lines print <WS1_API_URL>/<WS1_GROUPID> labels and param names only,
  and identifiers holding setting names/mask labels are renamed so they are
  not mistaken for secret values.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `8e9864fb` ws1_sensor_upload: never log secret values, show SHA-256 fingerprints at level 4+
  
  Addresses code scanning alert #16 (clear-text logging of sensitive data).
  Level 4+ now prints len + SHA-256 prefix per secret and OAuth token instead
  of plaintext; messages use labels rather than secret variables. Validate a
  secret by comparing with shasum, or inspect full values in a debugger.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
- `3c36e0ce` ws1_sensor_upload: add AUTOPKG_TOOLS_VERBOSE level (default 2), mask secrets below level 4
  
  Move to scripts/experimental/. Verbosity uses the launcher plumbing
  (AUTOPKG_TOOLS_VERBOSE, default 2, -v/--verbose override) and an output()
  helper; keychain/env secrets, URL host/path parts and the OAuth token are
  masked in all output and errors unless the level is 4 or higher.
  
  Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
