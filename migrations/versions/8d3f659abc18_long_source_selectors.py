"""Keep valid deep CSS selectors without truncation."""
from alembic import op
import sqlalchemy as sa

revision = '8d3f659abc18'
down_revision = '7c2e5489ab07'
branch_labels = None
depends_on = None
FIELDS = ('list_selector', 'title_selector', 'link_selector', 'date_selector', 'content_selector')


def upgrade():
    # SQLite does not enforce VARCHAR width. Do not rebuild this referenced
    # table merely to change its declared width: existing values stay intact.
    if op.get_bind().dialect.name == 'sqlite':
        return
    for field in FIELDS:
        op.alter_column('departments', field, existing_type=sa.String(500), type_=sa.String(4096))


def downgrade():
    connection = op.get_bind()
    condition = ' OR '.join('length(' + field + ') > 500' for field in FIELDS)
    if connection.execute(sa.text('SELECT COUNT(*) FROM departments WHERE ' + condition)).scalar_one():
        raise RuntimeError('仍有超过旧版长度限制的来源规则，不能无损降级')
    if connection.dialect.name == 'sqlite':
        return
    for field in FIELDS:
        op.alter_column('departments', field, existing_type=sa.String(4096), type_=sa.String(500))
