# Keep your current account and clips during upgrades

The current installation on James's computer is:
`C:\Users\james\Documents\Codex\2026-09-09\eve\outputs\kickclipper-demo-unlimited\kick-clipper`

Keep this installation as the source of truth. Its `kickclipper.db` holds accounts,
job ownership and clip indexes; `.env` holds its private configuration; `output`
holds videos, transcripts and editing metadata. Updating code must preserve all
three together. Do not run configure_local.py to replace an existing account.

Before moving to another computer, stop the app and its workers, then privately
copy the database, `.env`, and the entire `output` directory alongside the new
code. Do not share those private files in a public ZIP. The portable source ZIP
intentionally excludes them and on its own creates a separate, fresh installation.
Editing recipes may refer to old absolute source paths; update those paths when
moving computers. Keep the complete clip folders and source files.

September 13 repair: create the work/reviews directory on recovery, retain UTF-8
log decoding, and allow explicit recovery from saved transcript plus moment
checkpoint. Optional review failure preserves original scores. Export concurrency
is one to reduce pressure on this laptop. Reviewed CPU, prompt and title changes
from the accuracy ZIP are merged; the existing balanced scoring weights remain.
