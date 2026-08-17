"""In-memory CRM for automated tests.

Keyed on the normalized phone number, mirroring the [D11] identity rule, so a
second sync for the same phone updates rather than duplicates. Its failure mode is
what Phase 12 needs most: tests can make the CRM fail and assert the rest of the
platform carries on regardless (§14).
"""

from __future__ import annotations

import itertools

from app.core.errors import RetryableProviderError
from app.integrations.crm.provider import ContactUpsert, CRMContact, CRMProvider


class FakeCRMProvider(CRMProvider):
    name = "fake"

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.contacts_by_phone: dict[str, CRMContact] = {}
        self.upserts: list[ContactUpsert] = []
        self._counter = itertools.count(1)

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def find_contact(
        self, *, phone_e164: str | None = None, email: str | None = None
    ) -> CRMContact | None:
        self._maybe_fail()
        if phone_e164 and phone_e164 in self.contacts_by_phone:
            return self.contacts_by_phone[phone_e164]
        if email:
            for contact in self.contacts_by_phone.values():
                if contact.email == email:
                    return contact
        return None

    async def upsert_contact(self, upsert: ContactUpsert) -> CRMContact:
        self._maybe_fail()
        self.upserts.append(upsert)

        existing = self.contacts_by_phone.get(upsert.phone_e164)
        if existing is None:
            contact = CRMContact(
                provider_contact_id=f"fake-contact-{next(self._counter)}",
                phone_e164=upsert.phone_e164,
                email=upsert.email,
                properties=dict(upsert.properties),
            )
        else:
            merged = {**existing.properties, **upsert.properties}
            contact = CRMContact(
                provider_contact_id=existing.provider_contact_id,
                phone_e164=existing.phone_e164,
                email=upsert.email or existing.email,
                properties=merged,
            )

        self.contacts_by_phone[upsert.phone_e164] = contact
        return contact

    # ---------------------------------------------------------------- test helpers

    @property
    def contact_count(self) -> int:
        return len(self.contacts_by_phone)

    @property
    def upsert_count(self) -> int:
        return len(self.upserts)

    def reset(self) -> None:
        self.contacts_by_phone.clear()
        self.upserts.clear()

    def fail_retryably(self, message: str = "simulated HubSpot outage") -> None:
        self.fail_with = RetryableProviderError(message, provider=self.name)

    def stop_failing(self) -> None:
        self.fail_with = None
