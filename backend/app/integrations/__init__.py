"""External integrations, each behind an interface (CLAUDE.md).

Six concerns live here: WhatsApp, LLM, Embedding, Calendar, CRM and Queue. Every one
has an interface plus a fake used by automated tests. No layer above this package
names a concrete provider; selection happens through configuration and
`app.integrations.registry`.

Phase 1 ships interfaces and fakes only. Real providers arrive in their own phases:
Meta WhatsApp (6), Cloud Tasks (7), local Sentence Transformers (8), OpenRouter (10),
Google Calendar (11), HubSpot (12).
"""
