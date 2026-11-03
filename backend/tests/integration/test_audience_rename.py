"""Renaming an audience: a label change that touches nothing else."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import Audience, Lead
from tests.integration.conftest import login
from tests.integration.helpers import run_db, scoped_prefix
from tests.integration.test_campaigns_api import seed_audience

pytestmark = pytest.mark.integration

PHONES = ["9876543210", "9876543211"]


@pytest.fixture(autouse=True)
def _clean(seeded_users: dict[str, str]) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))

    run_db(clear)
    yield
    run_db(clear)


def rename(client: TestClient, audience_id: str, name: str) -> Response:
    response: Response = client.patch(
        f"{scoped_prefix()}/audiences/{audience_id}", json={"name": name}
    )
    return response


class TestRename:
    def test_an_admin_can_rename(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)

        response = rename(client, audience_id, "  Providers + Team - 02 Nov  ")

        assert response.status_code == 200
        assert response.json()["name"] == "Providers + Team - 02 Nov"
        shown = client.get(f"{scoped_prefix()}/audiences/{audience_id}").json()
        assert shown["name"] == "Providers + Team - 02 Nov"

    def test_renaming_keeps_the_contacts_and_status(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        before = client.get(f"{scoped_prefix()}/audiences/{audience_id}").json()

        rename(client, audience_id, "Renamed")

        after = client.get(f"{scoped_prefix()}/audiences/{audience_id}").json()
        assert after["status"] == before["status"]
        assert after["valid_rows"] == before["valid_rows"]

    def test_a_blank_name_is_refused(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        assert rename(client, audience_id, "   ").status_code == 422
        assert rename(client, audience_id, "").status_code == 422

    def test_a_salesperson_cannot_rename(self, client: TestClient) -> None:
        login(client)
        audience_id = seed_audience(client, PHONES)
        login(client, "sales@example.com")
        assert rename(client, audience_id, "Nope").status_code == 403

    def test_an_unknown_audience_is_not_found(self, client: TestClient) -> None:
        login(client)
        missing = "00000000-0000-0000-0000-000000000000"
        assert rename(client, missing, "X").status_code == 404
