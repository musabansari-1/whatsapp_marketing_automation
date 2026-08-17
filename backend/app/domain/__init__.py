"""Domain layer: business rules, independent of transport and persistence.

Empty in Phase 1 by design. The rules that belong here are owned by later phases, and
inventing them now would mean inventing requirements:

- state machines for campaign, lead, conversation, booking and message (Phase 2/5)
- Excel validation and phone normalization (Phase 4), once the client confirms the
  column schema ([D4]/[D20])
- opt-out keyword matching and suppression rules (Phase 6, [D5]/[D16])
- chunking and retrieval policy (Phase 8)
- qualification rules applied to `leads.qualification_data` (Phase 10, [D9]/[D18])
- slot computation from the [D8] slot policy and calendar busy intervals (Phase 11)

Rules in this layer must not import from `app.api`, `app.worker` or any concrete
provider; they operate on the interfaces and dataclasses those layers pass in.
"""
