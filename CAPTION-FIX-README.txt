WINDOWS CAPTION EXPORT FIX — 11 SEPTEMBER 2026

The failing command used ass=output\vod\Sneako\clips\...\clip.ass.
FFmpeg treats those backslashes as escapes inside a filter expression, so
libass looked for outputvodSneakoclips... instead of the actual file.
The fixed clip.py routes subtitle paths through ass_filter(), which resolves
the path and escapes the Windows drive colon for FFmpeg.

The VOD exporter now counts successful renders, not selected candidates.
It exits with an error if any exports fail, and only prints PROGRESS done
when all requested exports succeed.

INSTALL
Stop the app and processing jobs, then install this updated project over
the existing project while preserving .env, kickclipper.db, output and venv.
Install-Upgrade.ps1 accepts -Destination for a different installation path.
Restart the app after updating; already-running processes retain old code.

RECOVER WITHOUT REPROCESSING THE VOD
Keep the failed job's original work segments and clip.ass files. Save its
plain-text job log (including the full failed Command [...] lines) as
failed-log.txt in the project folder. In CMD, from that same project:

  venv\Scripts\python.exe retry_failed_exports.py failed-log.txt

This checks which exports can be retried. To perform those retries:

  venv\Scripts\python.exe retry_failed_exports.py failed-log.txt --apply

The tool supports the center-crop FFmpeg commands shown in the reported
error, skips already-playable videos, and only replaces a broken/missing
clip after producing a valid replacement. It does not transcribe, score,
download video, or call an AI provider. It leaves existing titles untouched
and adds a review placeholder only if a recovered clip has no titles.

Use the log and caption/source files from the SAME run. If they were
overwritten by a later run, don't combine them; restore that run's saved
files or reprocess it. Newer full-project builds use unique run folders to
reduce this risk.
