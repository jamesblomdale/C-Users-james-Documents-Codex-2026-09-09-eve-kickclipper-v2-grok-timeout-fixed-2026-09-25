# R4 recall/cut/oracle refinement

Implemented: separate Moment/Cut scoring metadata and hard gates; hook/payoff-aware cut scoring; niche hunting prompt; recap/cold-open guidance; richer clip cards; optional competitor-oracle clustering helpers.

Research basis: Twitch has an official Get Clips API by broadcaster; Kick web clip endpoints exist in community-maintained endpoint maps but are unofficial/fragile; long-video RAG work favors timestamped multimodal evidence, hierarchical/spatiotemporal retrieval and reranking over flattening an entire long video into one prompt. The oracle is deliberately optional so endpoint failures cannot break a VOD job.

Not silently implemented: production X scraping, automatic Kick-private endpoint dependence, or cross-VOD wall-clock alignment without source metadata. Those require provider/API credentials and fixtures. See CODEX-FURTHER-REFINEMENT-PROMPT.txt.
