# Performance verification — 15 September 2026

Measured on this computer with the configured Together account and a preserved
N3on source. A 600-second, 16 kHz mono MP3 chunk was 3.43 MB. Together Parakeet
returned 1,779 timestamped words in 6.77 seconds at configured concurrency 4.
The one-second credential/model preflight completed in 2.58 seconds.

The production profile uses compact 48 kbps AAC ingest, 600-second Parakeet
chunks with four seconds overlap, four concurrent ASR requests, six concurrent
scout batches, deep batches of eight across four workers, and two render workers.
Provider sessions reuse TLS connections and ignore accidental shell proxy values.

A failed ASR request no longer immediately cancels the other chunks. Successful
chunks finish and checkpoint, failed chunks retry conservatively, and an unresolved
failure reports exactly which chunks remain. Restarting the same source reuses the
successful checkpoints.

This measurement verifies the ASR path, not a full three-hour completion time.
HLS source throughput, Grok latency/rate limits, number of candidates, and selected
render settings still determine total wall time. Record the stage timing files for
three complete production runs before treating ten minutes as an SLA.
