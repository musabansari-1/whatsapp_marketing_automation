"""The internal-authoring product resolver (Phase 19, §28).

`POST /audiences/upload` gained a product. It is optional only for backward
compatibility: while exactly one active product exists a silent request is unambiguous
in fact, which is what keeps Academicae's existing behaviour working ([D54]).

What matters is *why* it resolves. It must be because one active product exists, not
because Academicae is first, oldest, or called what it is called — so these tests make a
second product and watch the same request start failing.

Products are created dynamically. SustainX and Life Herba are deliberately not seeded.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator, Sequence

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.main import API_V1_PREFIX
from app.domain.product import ProductContextError, resolve_authoring_product
from app.persistence.models import Audience, AudienceContact, Lead, Product, Segment
from tests.integration.conftest import login
from tests.integration.helpers import run_db

pytestmark = pytest.mark.integration

XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PHONE = "9876533000"


def make_xlsx(rows: Sequence[Sequence[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _clean(seeded_users: dict[str, str]) -> Iterator[None]:
    async def clear(session: AsyncSession) -> None:
        await session.execute(delete(AudienceContact))
        await session.execute(delete(Lead))
        await session.execute(delete(Audience))
        await session.execute(delete(Segment))
        await session.execute(delete(Product).where(Product.slug.like("t-%")))

    run_db(clear)
    yield
    run_db(clear)


@pytest.fixture
def admin(
    client: TestClient, seeded_users: dict[str, str], seeded_product: uuid.UUID
) -> TestClient:
    """Signed in, with the one pre-existing product present.

    `seeded_product` stands in for the row the migration seeds: the suite builds its
    schema with `create_all`, so without it there is no active product and the
    compatibility path has nothing to resolve to.
    """
    login(client)
    return client


def upload(
    client: TestClient, name: str, *, product_id: uuid.UUID | None = None, **extra: str
) -> tuple[int, dict[str, object]]:
    data: dict[str, str] = {"name": name, **extra}
    if product_id is not None:
        data["product_id"] = str(product_id)

    response = client.post(
        f"{API_V1_PREFIX}/audiences/upload",
        files={
            "file": (
                f"{name}.xlsx",
                make_xlsx([["Name", "Phone"], ["Person", PHONE]]),
                XLSX_TYPE,
            )
        },
        data=data,
    )
    body = response.json() if response.content else {}
    return response.status_code, body


def set_active(product_id: uuid.UUID, active: bool) -> None:
    async def update(session: AsyncSession) -> None:
        product = await session.get(Product, product_id)
        assert product is not None
        product.is_active = active

    run_db(update)


def sole_seeded_product() -> uuid.UUID:
    """Whatever the one pre-existing product is, without naming it."""

    async def read(session: AsyncSession) -> uuid.UUID:
        result = await session.execute(
            select(Product.id).where(Product.slug.not_like("t-%")).order_by(Product.created_at)
        )
        ids = list(result.scalars().all())
        assert len(ids) == 1, "these tests assume one pre-existing product"
        return ids[0]

    return run_db(read)


class TestTheRuleItself:
    """Pure logic, no database. The rule is small enough to state exactly."""

    def test_an_explicit_choice_always_wins(self) -> None:
        chosen = uuid.uuid4()
        others = [uuid.uuid4(), uuid.uuid4()]
        assert resolve_authoring_product(explicit=chosen, active_product_ids=others) == chosen

    def test_one_active_product_resolves_silently(self) -> None:
        only = uuid.uuid4()
        assert resolve_authoring_product(explicit=None, active_product_ids=[only]) == only

    def test_no_active_product_is_an_error(self) -> None:
        with pytest.raises(ProductContextError):
            resolve_authoring_product(explicit=None, active_product_ids=[])

    def test_several_active_products_is_an_error(self) -> None:
        """Never "the first one". That would be a default product by another name."""
        with pytest.raises(ProductContextError):
            resolve_authoring_product(
                explicit=None, active_product_ids=[uuid.uuid4(), uuid.uuid4()]
            )


class TestTheUploadEndpoint:
    def test_a_legacy_request_succeeds_while_one_product_is_active(self, admin: TestClient) -> None:
        """[D54]: Academicae's existing call, unchanged, still works."""
        status_code, body = upload(admin, "legacy")
        assert status_code == 201, body

        audience_id = uuid.UUID(str(body["audience"]["id"]))  # type: ignore[index]

        async def read(session: AsyncSession) -> uuid.UUID | None:
            audience = await session.get(Audience, audience_id)
            return audience.product_id if audience else None

        assert run_db(read) == sole_seeded_product()

    def test_it_resolves_by_count_not_by_identity(self, admin: TestClient) -> None:
        """The distinction the whole design turns on.

        A second *active* product makes the same silent request fail. If resolution were
        "fall back to the first product", it would keep succeeding — so this failing is
        the proof that it does not.
        """
        assert upload(admin, "before")[0] == 201

        second = make_product("t-second")
        assert upload(admin, "after")[0] == 400

        # Deactivating it restores the unambiguous state, which shows the rule reads
        # *active* products rather than merely counting rows.
        set_active(second, False)
        assert upload(admin, "deactivated")[0] == 201

    def test_an_explicit_product_keeps_working_with_several(self, admin: TestClient) -> None:
        second = make_product("t-second")

        status_code, body = upload(admin, "explicit", product_id=second)
        assert status_code == 201, body

        audience_id = uuid.UUID(str(body["audience"]["id"]))  # type: ignore[index]

        async def read(session: AsyncSession) -> uuid.UUID | None:
            audience = await session.get(Audience, audience_id)
            return audience.product_id if audience else None

        assert run_db(read) == second

    def test_an_unknown_product_is_404(self, admin: TestClient) -> None:
        assert upload(admin, "ghost", product_id=uuid.uuid4())[0] == 404

    def test_an_inactive_product_is_refused(self, admin: TestClient) -> None:
        dormant = make_product("t-dormant")
        set_active(dormant, False)
        assert upload(admin, "dormant", product_id=dormant)[0] == 422

    def test_a_segment_from_another_product_is_refused(self, admin: TestClient) -> None:
        """§28.8. The database refuses the pairing too; this is the readable version."""
        first = make_product("t-first")
        second = make_product("t-second")
        foreign = make_segment(second, "government")

        status_code, body = upload(admin, "crossed", product_id=first, segment_id=str(foreign))
        assert status_code == 422
        assert "different product" in str(body.get("detail", "")).lower()

    def test_a_segment_of_the_same_product_is_accepted(self, admin: TestClient) -> None:
        product = make_product("t-first")
        segment = make_segment(product, "government")

        status_code, body = upload(admin, "targeted", product_id=product, segment_id=str(segment))
        assert status_code == 201, body

        audience_id = uuid.UUID(str(body["audience"]["id"]))  # type: ignore[index]

        async def read(session: AsyncSession) -> uuid.UUID | None:
            audience = await session.get(Audience, audience_id)
            return audience.segment_id if audience else None

        assert run_db(read) == segment

    def test_a_product_wide_audience_is_valid(self, admin: TestClient) -> None:
        """No segment is not a missing value; a product may have none at all."""
        product = make_product("t-first")
        status_code, body = upload(admin, "wide", product_id=product)
        assert status_code == 201, body

        audience_id = uuid.UUID(str(body["audience"]["id"]))  # type: ignore[index]

        async def read(session: AsyncSession) -> uuid.UUID | None:
            audience = await session.get(Audience, audience_id)
            return audience.segment_id if audience else None

        assert run_db(read) is None


def make_product(slug: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        product = Product(slug=slug, display_name=slug.upper())
        session.add(product)
        await session.flush()
        return product.id

    return run_db(create)


def make_segment(product_id: uuid.UUID, slug: str) -> uuid.UUID:
    async def create(session: AsyncSession) -> uuid.UUID:
        segment = Segment(product_id=product_id, slug=slug, display_name=slug.title())
        session.add(segment)
        await session.flush()
        return segment.id

    return run_db(create)
