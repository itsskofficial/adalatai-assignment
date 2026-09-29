"""What the People screen shows and does: who may sign in, and what each person may do.

Every route here is for administrators only. The rules live in People; these routes turn
its refusals into answers with a plain message.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi import Path as PathParameter
from pydantic import BaseModel

from invoice_collector.api.people import (
    People,
    PeopleAction,
    PersonEntry,
    PersonRefused,
    Role,
    role,
)

PersonDependency = Callable[..., str]
AddressPath = Annotated[str, PathParameter(description="A person's email address")]


class PersonView(BaseModel):
    address: str
    role: Role
    set_by_installation: bool
    added_by: str | None
    added_at: str | None
    last_signed_in_at: str | None


class NewPerson(BaseModel):
    """The types are loose so that a wrong value is refused with a plain message."""

    address: str = ""
    role: str = "member"


class RoleChange(BaseModel):
    role: str = ""


class PeopleChangeView(BaseModel):
    address: str
    action: PeopleAction
    role_before: Role | None
    role_after: Role | None
    person: str
    changed_at: str


class RefusedSignInView(BaseModel):
    address: str
    attempted_at: str


def _time(at: datetime | None) -> str | None:
    return at.isoformat() if at is not None else None


def _view(entry: PersonEntry) -> PersonView:
    return PersonView(
        address=entry.address,
        role=entry.role,
        set_by_installation=entry.set_by_installation,
        added_by=entry.added_by,
        added_at=_time(entry.added_at),
        last_signed_in_at=_time(entry.last_signed_in_at),
    )


def _refused(refused: PersonRefused) -> HTTPException:
    return HTTPException(status_code=refused.status, detail=str(refused))


def people_routes(
    people: People,
    administrator: PersonDependency,
    now: Callable[[], datetime],
) -> APIRouter:
    router = APIRouter(prefix="/people")

    @router.get("")
    def everyone(_: Annotated[str, Depends(administrator)]) -> list[PersonView]:  # pyright: ignore[reportUnusedFunction]
        return [_view(entry) for entry in people.everyone()]

    @router.post("", status_code=201)
    def add(person: Annotated[str, Depends(administrator)], new: NewPerson) -> PersonView:  # pyright: ignore[reportUnusedFunction]
        try:
            return _view(people.add(new.address, role(new.role), by=person, at=now()))
        except PersonRefused as refused:
            raise _refused(refused) from None

    @router.get("/history")
    def history(_: Annotated[str, Depends(administrator)]) -> list[PeopleChangeView]:  # pyright: ignore[reportUnusedFunction]
        return [
            PeopleChangeView(
                address=change.address,
                action=change.action,
                role_before=change.role_before,
                role_after=change.role_after,
                person=change.person,
                changed_at=change.changed_at.isoformat(),
            )
            for change in people.history()
        ]

    @router.get("/refused")
    def refused(_: Annotated[str, Depends(administrator)]) -> list[RefusedSignInView]:  # pyright: ignore[reportUnusedFunction]
        return [
            RefusedSignInView(
                address=attempt.address, attempted_at=attempt.attempted_at.isoformat()
            )
            for attempt in people.refusals()
        ]

    @router.put("/{address}")
    def change_role(  # pyright: ignore[reportUnusedFunction]
        address: AddressPath, person: Annotated[str, Depends(administrator)], change: RoleChange
    ) -> PersonView:
        try:
            return _view(people.change_role(address, role(change.role), by=person, at=now()))
        except PersonRefused as refused:
            raise _refused(refused) from None

    @router.delete("/{address}", status_code=204)
    def remove(address: AddressPath, person: Annotated[str, Depends(administrator)]) -> Response:  # pyright: ignore[reportUnusedFunction]
        try:
            people.remove(address, by=person, at=now())
        except PersonRefused as refused:
            raise _refused(refused) from None
        return Response(status_code=204)

    return router
