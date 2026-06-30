# Sync Changelog — 2026-06-30

**7 commit(s)** since last sync.

## New Features & Enhancements

- `6115bd7b` add Slack notification for newly created Trust Info PR with review link
- `d402e7d2` add clickable GitHub commit links to trust failure notifications
- `27aaf20d` add logic to update or create pull requests with new trust info body
- `b83eae64` add autopkg_prefs.plist to .gitignore
- `9f225e8c` enhance git branch management in autopkg_ws1-car.yml...
  
   to account for handling multiple runs on same day, generating / updating / overwriting a trust-info-PR to approve changes in parent recipe chain

## Other Changes

- `22c15953` shorten Slack notifications for trust failures...
  
  update Slack notifications to include only concise commit links for trust failures, if available
- `66196ee5` ensure maximum verbosity for trust verification in CAR mode
