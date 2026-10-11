"""Keep school request/token budgets across task generations."""
from alembic import op
import sqlalchemy as sa

revision = '9a2e51b72086'
down_revision = '891d40a61f75'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ai_budgets', sa.Column('used_requests', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('ai_budgets', sa.Column('reserved_requests', sa.Integer(), nullable=False, server_default='0'))
    op.add_column('ai_executions', sa.Column('request_sent', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('ai_school_rounds',
        sa.Column('school_id', sa.Integer(), primary_key=True),
        sa.Column('budget_key', sa.String(48), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime()),
        sa.Column('completed_structure', sa.JSON(), nullable=False, server_default='{}'))
    # Retain already-dispatched spend when upgrading. Unknown results are never replayed.
    executions = sa.table('ai_executions', sa.column('execution_id'), sa.column('status'),
        sa.column('error_code'), sa.column('budget_keys', sa.JSON()), sa.column('request_sent', sa.Boolean()))
    budgets = sa.table('ai_budgets', sa.column('key'), sa.column('used_requests'), sa.column('reserved_requests'))
    connection = op.get_bind()
    counts = {}
    not_dispatched = {'credential_unavailable', 'configuration_changed', 'profile_disabled',
                      'profile_deleted', 'not_configured', 'profile_not_tested'}
    for ident, status, error_code, keys in connection.execute(sa.select(executions.c.execution_id,
            executions.c.status, executions.c.error_code, executions.c.budget_keys)):
        # Explicit pre-dispatch configuration failures are not actual requests.
        # Other historical failures are conservatively counted: zero returned
        # usage does not prove a POST was never sent. Token ledgers stay intact.
        sent = status != 'reserved' and error_code not in not_dispatched
        connection.execute(executions.update().where(executions.c.execution_id == ident).values(request_sent=sent))
        for key in keys or []:
            count = counts.setdefault(key, [0, 0])
            if sent:
                count[0] += 1
            elif status == 'reserved':
                count[1] += 1
    for key, (used, reserved) in counts.items():
        connection.execute(budgets.update().where(budgets.c.key == key).values(
            used_requests=used, reserved_requests=reserved))


def downgrade():
    op.drop_table('ai_school_rounds')
    with op.batch_alter_table('ai_executions') as batch:
        batch.drop_column('request_sent')
    with op.batch_alter_table('ai_budgets') as batch:
        batch.drop_column('reserved_requests')
        batch.drop_column('used_requests')
