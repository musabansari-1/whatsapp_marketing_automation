"""Knowledge base API tests (§9, §11, §16, §17, §19).

Drives the endpoints through the app, so authentication, validation and upload limits
are exercised as a client would meet them.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DocumentStatus
from app.persistence.models import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionJob,
)
from tests.integration.conftest import SALES_EMAIL, login
from tests.integration.helpers import run_db, scoped_prefix

pytestmark = pytest.mark.integration


def base_path() -> str:
    """This test's product's knowledge bases. Knowledge exists only inside a product."""
    return f"{scoped_prefix()}/knowledge-bases"


@pytest.fixture(autouse=True)
def clean_knowledge(engine: Any) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(KnowledgeChunk))
        await session.execute(delete(KnowledgeIngestionJob))
        await session.execute(delete(KnowledgeDocument))
        await session.execute(delete(KnowledgeBase))

    run_db(clear)
    yield
    run_db(clear)


def create_base(client: TestClient, name: str = "Company Handbook") -> str:
    response = client.post(base_path(), json={"name": name, "description": "Internal reference"})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def upload(
    client: TestClient,
    base_id: str,
    *,
    filename: str = "policy.txt",
    content: bytes = b"Our refund window is thirty days.",
    content_type: str = "text/plain",
) -> Any:
    return client.post(
        f"{base_path()}/{base_id}/documents",
        files={"file": (filename, io.BytesIO(content), content_type)},
    )


class TestAuthentication:
    def test_every_endpoint_requires_a_session(self, client: TestClient) -> None:
        """§9: company documents are not public."""
        assert client.get(base_path()).status_code == 401
        assert client.post(base_path(), json={"name": "x", "description": None}).status_code == 401
        assert upload(client, str(uuid.uuid4())).status_code == 401

    def test_a_salesperson_may_read_the_knowledge_base(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Reading is not the restricted operation. Changing knowledge is (D-07)."""
        login(client, email=SALES_EMAIL)
        assert client.get(base_path()).status_code == 200


class TestOnlyAdministratorsChangeKnowledge:
    """D-07, decided for launch in Phase 27: knowledge is what the assistant tells
    customers, so creating, uploading and archiving are ADMIN-only. Reading stays open."""

    def test_a_salesperson_cannot_create_a_knowledge_base(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client, email=SALES_EMAIL)
        response = client.post(base_path(), json={"name": "Sales notes", "description": None})
        assert response.status_code == 403

    def test_a_salesperson_cannot_upload_or_archive_but_can_read(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        base_id = create_base(client)
        uploaded = upload(client, base_id)
        assert uploaded.status_code == 201, uploaded.text
        document_id = uploaded.json()["id"]

        login(client, email=SALES_EMAIL)

        assert upload(client, base_id, content=b"A salesperson's addition.").status_code == 403
        archive = client.post(f"{base_path()}/{base_id}/documents/{document_id}/archive")
        assert archive.status_code == 403
        assert client.get(f"{base_path()}/{base_id}/documents").status_code == 200
        assert client.get(f"{base_path()}/{base_id}/documents/{document_id}").status_code == 200


class TestKnowledgeBases:
    def test_a_knowledge_base_is_created_and_listed(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        base_id = create_base(client)

        listed = client.get(base_path())
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [base_id]

    def test_a_duplicate_name_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Two knowledge bases with the same name are indistinguishable in the
        dashboard and in an AI tool call."""
        login(client)
        create_base(client, "Handbook")

        assert (
            client.post(base_path(), json={"name": "handbook", "description": None}).status_code
            == 409
        )

    def test_a_missing_knowledge_base_is_404(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.get(f"{base_path()}/{uuid.uuid4()}").status_code == 404

    def test_an_empty_name_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert client.post(base_path(), json={"name": "  ", "description": None}).status_code == 422


class TestUpload:
    def test_a_document_is_accepted_as_pending(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """§11 makes ingestion asynchronous, so the response says PENDING rather than
        pretending the document is already searchable."""
        login(client)
        base_id = create_base(client)

        response = upload(client, base_id)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == DocumentStatus.PENDING.value
        assert body["title"] == "policy.txt"
        assert body["chunk_count"] == 0

    def test_ingestion_is_queued(self, client: TestClient, seeded_users: dict[str, str]) -> None:
        login(client)
        base_id = create_base(client)
        document_id = upload(client, base_id).json()["id"]

        async def job(session: AsyncSession) -> KnowledgeIngestionJob | None:
            from app.persistence.repositories import knowledge as knowledge_repo

            return await knowledge_repo.get_job_for_document(session, uuid.UUID(document_id))

        queued = run_db(job)
        assert queued is not None
        # Deterministic, so a redelivered task collapses instead of re-ingesting (§20).
        assert queued.task_id == f"ingest-{document_id}"

    @pytest.mark.parametrize("filename", ["notes.docx", "data.xlsx", "photo.png", "noextension"])
    def test_an_unsupported_format_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str], filename: str
    ) -> None:
        """Rejected at the door rather than failing asynchronously, so the uploader
        learns immediately."""
        login(client)
        base_id = create_base(client)

        assert upload(client, base_id, filename=filename).status_code == 415

    def test_an_empty_file_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        base_id = create_base(client)

        assert upload(client, base_id, content=b"").status_code == 400

    def test_an_oversized_file_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§19 requires an upload limit; without one a single file can exhaust the
        worker's memory during embedding."""
        login(client)
        base_id = create_base(client)

        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "knowledge_max_file_bytes", 32)

        assert upload(client, base_id, content=b"x" * 100).status_code == 413

    def test_identical_content_is_rejected(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Re-ingesting the same bytes doubles every chunk in the retrieval pool and
        skews results toward whatever was uploaded twice."""
        login(client)
        base_id = create_base(client)
        upload(client, base_id)

        duplicate = upload(client, base_id, filename="copy.txt")
        assert duplicate.status_code == 409

    def test_the_same_content_may_live_in_another_knowledge_base(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """De-duplication is per knowledge base: two bases are separate retrieval
        pools and a shared policy belongs in both."""
        login(client)
        first = create_base(client, "Sales")
        second = create_base(client, "Support")

        assert upload(client, first).status_code == 201
        assert upload(client, second).status_code == 201

    def test_uploading_to_a_missing_knowledge_base_is_404(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        assert upload(client, str(uuid.uuid4())).status_code == 404

    def test_a_markdown_document_is_accepted(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        base_id = create_base(client)

        response = upload(
            client, base_id, filename="guide.md", content=b"# Guide\n\nRefunds take 30 days."
        )
        assert response.status_code == 201


class TestDocuments:
    def test_documents_are_listed_and_fetched(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        base_id = create_base(client)
        document_id = upload(client, base_id).json()["id"]

        listed = client.get(f"{base_path()}/{base_id}/documents")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [document_id]

        fetched = client.get(f"{base_path()}/{base_id}/documents/{document_id}")
        assert fetched.status_code == 200
        assert fetched.json()["id"] == document_id

    def test_a_document_from_another_knowledge_base_is_not_reachable(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Checked rather than assumed: guessing the pair must not expose a document
        that belongs elsewhere."""
        login(client)
        first = create_base(client, "Sales")
        second = create_base(client, "Support")
        document_id = upload(client, first).json()["id"]

        assert client.get(f"{base_path()}/{second}/documents/{document_id}").status_code == 404

    def test_archiving_removes_a_document_from_the_default_listing(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        base_id = create_base(client)
        document_id = upload(client, base_id).json()["id"]

        archived = client.post(f"{base_path()}/{base_id}/documents/{document_id}/archive")
        assert archived.status_code == 200
        assert archived.json()["status"] == DocumentStatus.ARCHIVED.value

        assert client.get(f"{base_path()}/{base_id}/documents").json() == []
        included = client.get(f"{base_path()}/{base_id}/documents?include_archived=true")
        assert [item["id"] for item in included.json()] == [document_id]

    def test_archiving_twice_is_harmless(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """A double-clicked button must not become an error."""
        login(client)
        base_id = create_base(client)
        document_id = upload(client, base_id).json()["id"]

        client.post(f"{base_path()}/{base_id}/documents/{document_id}/archive")
        second = client.post(f"{base_path()}/{base_id}/documents/{document_id}/archive")

        assert second.status_code == 200
        assert second.json()["status"] == DocumentStatus.ARCHIVED.value

    def test_an_archived_document_can_be_uploaded_again(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Archiving is reversible by re-upload; the hash check ignores archived rows
        so a withdrawn document can be restored."""
        login(client)
        base_id = create_base(client)
        document_id = upload(client, base_id).json()["id"]
        client.post(f"{base_path()}/{base_id}/documents/{document_id}/archive")

        assert upload(client, base_id).status_code == 201

    def test_a_missing_document_is_404(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        login(client)
        base_id = create_base(client)
        assert client.get(f"{base_path()}/{base_id}/documents/{uuid.uuid4()}").status_code == 404


class TestNoRetrievalEndpoint:
    def test_retrieval_is_not_exposed_over_http(
        self, client: TestClient, seeded_users: dict[str, str]
    ) -> None:
        """Retrieval is an AI tool in Phase 10, not a dashboard feature. Exposing it
        now would invite use from places §9 does not permit."""
        login(client)
        base_id = create_base(client)

        for path in (f"{base_path()}/{base_id}/search", f"{base_path()}/{base_id}/retrieve"):
            assert client.post(path, json={"query": "refunds"}).status_code == 404
