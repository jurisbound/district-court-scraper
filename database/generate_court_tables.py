"""Regenerate 01_court_tables.sql from storage/models.py.

    python database/generate_court_tables.py

Run it whenever a model changes, so the setup SQL never drifts from the code,
then copy the new file to the `database/` folder of the other repositories.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # repository root

from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from storage.models import Base  # noqa: E402

HEADER = """-- 01_court_tables.sql
-- The court-data tables, generated from storage/models.py (SQLAlchemy),
-- including the (cnr, order_no, judgment_date) document-identity index.
-- Do not hand-edit: regenerate with  python database/generate_court_tables.py
-- Equivalent to Base.metadata.create_all() against an empty database.
"""


def main():
    # The complete table list is kept in the court-pipeline repository. A
    # repository whose storage/models.py lists fewer columns holds this file
    # as a reference copy: stop instead of writing a wrong file.
    columns = Base.metadata.tables["scraped_documents"].columns
    missing = [c for c in ("layout", "sensitive_case", "names_hidden", "doc_number") if c not in columns]
    if missing:
        raise SystemExit(f"storage/models.py here does not list {missing}. Run this script in court-pipeline, "
                         "then copy 01_court_tables.sql to the other repositories.")
    dialect = postgresql.dialect()
    out = [HEADER]
    for table in Base.metadata.sorted_tables:
        out.append(str(CreateTable(table).compile(dialect=dialect)).strip() + ";\n")
        for index in sorted(table.indexes, key=lambda i: i.name):
            out.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";")
        out.append("")
    target = HERE / "01_court_tables.sql"
    target.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {target.name}: {[t.name for t in Base.metadata.sorted_tables]}")


if __name__ == "__main__":
    main()
