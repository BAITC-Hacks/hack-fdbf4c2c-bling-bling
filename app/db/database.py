from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import URL, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from app.core.config import Settings


class Base(DeclarativeBase):
    pass


def build_engine(settings: Settings):
    return create_engine(
        URL.create("sqlite", database=str(settings.sqlite_path)),
        connect_args={"check_same_thread": False},
    )


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session
