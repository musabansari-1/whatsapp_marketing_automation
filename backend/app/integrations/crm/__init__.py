"""CRM integration package."""

from app.integrations.crm.fake import FakeCRMProvider
from app.integrations.crm.provider import ContactUpsert, CRMContact, CRMProvider

__all__ = [
    "CRMContact",
    "CRMProvider",
    "ContactUpsert",
    "FakeCRMProvider",
]
