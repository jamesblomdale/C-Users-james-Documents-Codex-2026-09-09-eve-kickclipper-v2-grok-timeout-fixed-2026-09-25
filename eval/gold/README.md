# Golden VOD labels

Create one JSON file per representative VOD. Labels must be chosen by a human
editor before comparing providers so the evaluation does not reward a model for
agreeing with itself.

```json
{
  "source_id": "stable-private-source-id",
  "labelled_by": "editor name",
  "labelled_at": "2026-09-16",
  "moments": [
    {
      "gold_start": 125.5,
      "gold_end": 181.0,
      "event_type": "argument",
      "why_it_is_good": "A challenge escalates and receives a decisive reaction.",
      "required_context": "The accusation immediately before the response.",
      "payoff_timestamp": 168.2
    }
  ]
}
```

Do not place private VOD URLs, credentials or full transcripts in these files.

