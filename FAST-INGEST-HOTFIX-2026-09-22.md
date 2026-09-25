# Fast ingest hotfix — 2026-09-22

- Raised default HLS fragment concurrency from 8 to 16 (bounded max 32).
- Added explicit yt-dlp HLS buffer sizing.
- Dashboard now shows current-stage percentage next to job-wide percentage.
- Suppressed wildly inaccurate whole-job ETA during the first few HLS progress samples.
- setup-wsl.sh detects and deletes stale transferred virtualenvs before recreating them.
- start-wsl.sh clears PYTHONHOME/PYTHONPATH and rejects a broken virtualenv instead of crashing with missing encodings.
- Reviewed the Sep-19 PATCHED build. Its CSRF/dashboard hotfix is valuable for the security-enabled branch, but was not blindly copied because this FAST branch does not currently install that security layer; copying only the frontend token code would break requests.

Validation in sandbox:
- compileall: PASS
- focused VOD/Parakeet/HLS/dashboard tests: 39/39 PASS
- full suite discovered 70 tests. Two Flask-dependent imports cannot run in the sandbox because Flask is unavailable here. Attempting to install it was blocked by sandbox DNS/network access, not by the project.
