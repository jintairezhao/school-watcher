"""Keep dialect-specific upserts out of business services."""
from backend.database.db import db


def insert(table):
    if db.engine.dialect.name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert as statement
    elif db.engine.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert as statement
    else:
        raise RuntimeError('Unsupported database dialect')
    return statement(table)
