"""Audience endpoint tests (specification §6, §16, §19).

The rule these exist to protect is the one §6 states outright: uploading must never
send a message or invoke the AI.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from openpyxl import Workbook
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.main import API_V1_PREFIX
from app.persistence.models import Audience, Lead
from tests.integration.conftest import login
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def make_xlsx(rows: Sequence[Sequence[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def upload(
    client: TestClient, content: bytes, *, filename: str = "audience.xlsx", name: str = ""
) -> Response:
    response: Response = client.post(
        f"{API_V1_PREFIX}/audiences/upload",
        files={"file": (filename, content, XLSX_TYPE)},
        data={"name": name},
    )
    return response


# The canonical V1 columns ([D25]): name and phone required, the rest optional.
SAMPLE = [
    ["Name", "Phone", "Email", "Company", "Designation", "City"],
    ["Rahul", "9876543210", "rahul@example.com", "Acme", "Head of Ops", "Mumbai"],
    ["Priya", "9876543211", "priya@example.com", "Globex", "Director", "Pune"],
]


@pytest.fixture(autouse=True)
def _clean_audiences(seeded_users: dict[str, str]) -> Iterator[None]:
    """Remove audiences and leads around each test.

    Committed data, since the API opens its own sessions; the rollback fixture used
    elsewhere would not be visible to it.
    """

    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))

    run_db(clear)
    yield
    run_db(clear)


class TestUploadRequiresAuthentication:
    def test_anonymous_upload_is_rejected(self, client: TestClient) -> None:
        assert upload(client, make_xlsx(SAMPLE)).status_code == 401

    def test_anonymous_list_is_rejected(self, client: TestClient) -> None:
        assert client.get(f"{API_V1_PREFIX}/audiences").status_code == 401

    def test_a_salesperson_may_upload(self, client: TestClient) -> None:
        """Uploading is not admin-only: §3 gives audience upload to the internal user
        generally, and [D14] restricts only user management."""
        login(client, "sales@example.com")
        assert upload(client, make_xlsx(SAMPLE)).status_code == 201


class TestUploadPreview:
    def test_upload_returns_the_preview(self, client: TestClient) -> None:
        login(client)
        response = upload(client, make_xlsx(SAMPLE))

        assert response.status_code == 201
        audience = response.json()["audience"]
        assert audience["total_rows"] == 2
        assert audience["valid_rows"] == 2
        assert audience["invalid_rows"] == 0
        assert audience["duplicate_rows"] == 0

    def test_upload_does_not_confirm_the_audience(self, client: TestClient) -> None:
        """§6: an admin reviews the preview before the audience is usable."""
        login(client)
        assert upload(client, make_xlsx(SAMPLE)).json()["audience"]["status"] == (
            "PENDING_CONFIRMATION"
        )

    def test_upload_creates_no_leads(self, client: TestClient) -> None:
        """Uploading must have no downstream effect — no leads, and by construction
        no messages and no AI, since this module reaches no provider."""
        login(client)
        upload(client, make_xlsx(SAMPLE))

        async def count(session: AsyncSession) -> int:
            return int((await session.execute(select(func.count()).select_from(Lead))).scalar_one())

        assert run_db(count) == 0

    def test_phone_numbers_are_normalized(self, client: TestClient) -> None:
        login(client)
        response = upload(
            client, make_xlsx([["Name", "Phone"], ["A", "9876543210"], ["B", "+91 98765 43211"]])
        )
        numbers = {row["phone_e164"] for row in response.json()["sample"]}
        assert numbers == {"+919876543210", "+919876543211"}

    def test_name_defaults_to_the_filename(self, client: TestClient) -> None:
        login(client)
        response = upload(client, make_xlsx(SAMPLE), filename="q3-list.xlsx")
        assert response.json()["audience"]["name"] == "q3-list.xlsx"

    def test_explicit_name_is_used(self, client: TestClient) -> None:
        login(client)
        response = upload(client, make_xlsx(SAMPLE), name="Q3 campaign list")
        assert response.json()["audience"]["name"] == "Q3 campaign list"


class TestRejectedRowsArePreserved:
    def test_invalid_and_duplicate_rows_are_stored(self, client: TestClient) -> None:
        """§6 requires the preview to show what was rejected and why."""
        login(client)
        content = make_xlsx(
            [
                ["Name", "Phone"],
                ["Valid", "9876543210"],
                ["Duplicate", "9876543210"],
                ["Invalid", "rubbish"],
            ]
        )
        audience = upload(client, content).json()["audience"]

        assert audience["total_rows"] == 3
        assert audience["valid_rows"] == 1
        assert audience["duplicate_rows"] == 1
        assert audience["invalid_rows"] == 1

    def test_contacts_can_be_filtered_by_status(self, client: TestClient) -> None:
        """An admin reviewing a rejected upload wants the bad rows, not the good ones."""
        login(client)
        content = make_xlsx([["Name", "Phone"], ["A", "9876543210"], ["B", "rubbish"]])
        audience_id = upload(client, content).json()["audience"]["id"]

        response = client.get(
            f"{API_V1_PREFIX}/audiences/{audience_id}/contacts",
            params={"validation_status": "INVALID"},
        )
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["validation_status"] == "INVALID"
        assert body["items"][0]["validation_error"]

    def test_original_row_is_kept(self, client: TestClient) -> None:
        login(client)
        content = make_xlsx([["Name", "Phone", "Region"], ["Rahul", "9876543210", "West"]])
        audience_id = upload(client, content).json()["audience"]["id"]

        contacts = client.get(f"{API_V1_PREFIX}/audiences/{audience_id}/contacts").json()
        assert contacts["items"][0]["raw_row"]["Region"] == "West"

    def test_contacts_are_paginated(self, client: TestClient) -> None:
        login(client)
        content = make_xlsx([["Name", "Phone"], *[[f"P{i}", f"98765{i:05d}"] for i in range(10)]])
        audience_id = upload(client, content).json()["audience"]["id"]

        page = client.get(
            f"{API_V1_PREFIX}/audiences/{audience_id}/contacts", params={"limit": 3, "offset": 0}
        ).json()
        assert len(page["items"]) == 3
        assert page["total"] == 10


class TestFileValidation:
    def test_missing_required_column_rejects_the_file(self, client: TestClient) -> None:
        """A wrong file, not bad data: rejected whole rather than row by row."""
        login(client)
        response = upload(client, make_xlsx([["Email", "Company"], ["r@example.com", "Acme"]]))

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "Name" in detail and "Phone" in detail

    def test_non_xlsx_extension_is_rejected(self, client: TestClient) -> None:
        login(client)
        assert upload(client, b"data", filename="list.csv").status_code == 415

    def test_corrupt_file_is_rejected(self, client: TestClient) -> None:
        login(client)
        assert upload(client, b"not really a spreadsheet").status_code == 400

    def test_empty_file_is_rejected(self, client: TestClient) -> None:
        login(client)
        assert upload(client, b"").status_code == 400

    def test_headers_without_rows_are_rejected(self, client: TestClient) -> None:
        login(client)
        response = upload(client, make_xlsx([["Name", "Phone"]]))
        assert response.status_code == 400
        assert "no data rows" in response.json()["detail"]


class TestSchemaEndpoint:
    def test_reports_the_configured_columns(self, client: TestClient) -> None:
        login(client)
        body = client.get(f"{API_V1_PREFIX}/audiences/schema").json()

        assert any(column["field"] == "phone" and column["required"] for column in body["columns"])
        assert body["phone_default_region"] == "IN"

    def test_flags_the_development_placeholder(self, client: TestClient) -> None:
        """[D4]/[D20]: the client's real schema is still outstanding, and the UI must
        not present a development default as authoritative."""
        login(client)
        assert client.get(f"{API_V1_PREFIX}/audiences/schema").json()["is_placeholder"] is True


class TestConfirmation:
    def test_confirming_promotes_valid_contacts_to_leads(self, client: TestClient) -> None:
        login(client)
        audience_id = upload(client, make_xlsx(SAMPLE)).json()["audience"]["id"]

        response = client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm")
        assert response.status_code == 200
        assert response.json()["audience"]["status"] == "CONFIRMED"
        assert response.json()["leads_upserted"] == 2

    def test_only_valid_rows_become_leads(self, client: TestClient) -> None:
        login(client)
        content = make_xlsx(
            [
                ["Name", "Phone"],
                ["A", "9876543210"],
                ["B", "9876543210"],
                ["C", "rubbish"],
            ]
        )
        audience_id = upload(client, content).json()["audience"]["id"]

        assert (
            client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm").json()["leads_upserted"]
            == 1
        )

    def test_confirming_twice_is_idempotent(self, client: TestClient) -> None:
        """A double click must not double-create leads."""
        login(client)
        audience_id = upload(client, make_xlsx(SAMPLE)).json()["audience"]["id"]

        client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm")
        client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm")

        async def count(session: AsyncSession) -> int:
            return int((await session.execute(select(func.count()).select_from(Lead))).scalar_one())

        assert run_db(count) == 2

    def test_the_same_number_in_two_audiences_yields_one_lead(self, client: TestClient) -> None:
        """[D11]: the normalized phone is the deduplication identity."""
        login(client)
        first = upload(client, make_xlsx([["Name", "Phone"], ["Rahul", "9876543210"]]))
        client.post(f"{API_V1_PREFIX}/audiences/{first.json()['audience']['id']}/confirm")

        second = upload(client, make_xlsx([["Name", "Phone"], ["Rahul K", "+91 98765 43210"]]))
        client.post(f"{API_V1_PREFIX}/audiences/{second.json()['audience']['id']}/confirm")

        async def leads(session: AsyncSession) -> list[tuple[str, str | None]]:
            rows = (await session.execute(select(Lead.phone_e164, Lead.full_name))).all()
            return [(row[0], row[1]) for row in rows]

        result = run_db(leads)
        assert len(result) == 1
        assert result[0][0] == "+919876543210"
        assert result[0][1] == "Rahul K"  # later upload updated the name

    def test_the_optional_canonical_fields_reach_the_lead(self, client: TestClient) -> None:
        """[D25]: promotion must not discard company, designation or city."""
        login(client)
        audience_id = upload(client, make_xlsx(SAMPLE)).json()["audience"]["id"]
        client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm")

        async def lead(session: AsyncSession) -> tuple[str | None, str | None, str | None]:
            row = (
                await session.execute(
                    select(Lead.company, Lead.designation, Lead.city).where(
                        Lead.phone_e164 == "+919876543210"
                    )
                )
            ).one()
            return row[0], row[1], row[2]

        assert run_db(lead) == ("Acme", "Head of Ops", "Mumbai")

    def test_a_later_blank_does_not_erase_existing_detail(self, client: TestClient) -> None:
        """A sparser second file must not wipe what the first supplied."""
        login(client)
        first = upload(client, make_xlsx([["Name", "Phone"], ["Rahul", "9876543210"]]))
        client.post(f"{API_V1_PREFIX}/audiences/{first.json()['audience']['id']}/confirm")

        second = upload(
            client, make_xlsx([["Name", "Phone", "Company"], ["Rahul", "9876543210", None]])
        )
        client.post(f"{API_V1_PREFIX}/audiences/{second.json()['audience']['id']}/confirm")

        async def company(session: AsyncSession) -> str | None:
            return (await session.execute(select(Lead.company))).scalar_one()

        # The first upload had no company either, so this asserts the coalesce did not
        # invent one; the name survived because both files carried it.
        assert run_db(company) is None

    def test_an_audience_with_no_valid_rows_cannot_be_confirmed(self, client: TestClient) -> None:
        login(client)
        audience_id = upload(client, make_xlsx([["Name", "Phone"], ["A", "rubbish"]])).json()[
            "audience"
        ]["id"]
        assert client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm").status_code == 422

    def test_confirming_sends_nothing(self, client: TestClient) -> None:
        """Confirmation makes the audience usable; §7 requires a separate, explicitly
        triggered campaign to send anything."""
        login(client)
        audience_id = upload(client, make_xlsx(SAMPLE)).json()["audience"]["id"]
        response = client.post(f"{API_V1_PREFIX}/audiences/{audience_id}/confirm")

        assert response.status_code == 200
        assert "message" not in response.json()


class TestRetrieval:
    def test_audiences_are_listed(self, client: TestClient) -> None:
        login(client)
        upload(client, make_xlsx(SAMPLE), name="First")
        upload(client, make_xlsx(SAMPLE), name="Second")

        names = {audience["name"] for audience in client.get(f"{API_V1_PREFIX}/audiences").json()}
        assert names == {"First", "Second"}

    def test_audience_detail(self, client: TestClient) -> None:
        login(client)
        audience_id = upload(client, make_xlsx(SAMPLE)).json()["audience"]["id"]

        response = client.get(f"{API_V1_PREFIX}/audiences/{audience_id}")
        assert response.status_code == 200
        assert response.json()["valid_rows"] == 2

    def test_unknown_audience_is_404(self, client: TestClient) -> None:
        login(client)
        assert client.get(f"{API_V1_PREFIX}/audiences/{uuid.uuid4()}").status_code == 404

    def test_contacts_for_unknown_audience_is_404(self, client: TestClient) -> None:
        login(client)
        assert client.get(f"{API_V1_PREFIX}/audiences/{uuid.uuid4()}/contacts").status_code == 404
