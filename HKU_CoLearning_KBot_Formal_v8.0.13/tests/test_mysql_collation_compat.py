import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from common.models import (
    NotesBookmark,
    NotesCorrection,
    NotesDetail,
    NotesFeedback,
    NotesQARecord,
)


@pytest.mark.parametrize(
    "model",
    (NotesQARecord, NotesDetail, NotesFeedback, NotesCorrection, NotesBookmark),
)
def test_notes_mysql_tables_share_legacy_collation(model):
    """New FK tables must match the collation of an existing v6.2 parent."""
    ddl = str(CreateTable(model.__table__).compile(dialect=mysql.dialect())).casefold()
    normalized = ddl.replace("=", " ")
    assert "charset utf8mb4" in normalized
    assert "collate utf8mb4_unicode_ci" in normalized
