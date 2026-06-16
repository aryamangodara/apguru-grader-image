"""Extend sub_topics with classification columns used by the weekly plan.

Adds four nullable columns to ``sub_topics`` so the weekly-plan
service can rank candidate subtopics by difficulty and frequency,
and present richer skill/domain context to the LLM:

- ``difficulty``           ENUM(easy, medium, hard)
- ``approximate_frequency`` ENUM(high, medium, low)
- ``skill``                VARCHAR(255)
- ``domain``               VARCHAR(255)

All four are nullable so the migration is safe to apply on an
already-populated table — the companion population script
``scripts/classify_sub_topics.py`` fills them in via LLM
classification. ROI ranking in the service treats NULLs as
"unknown / neutral".

Revision ID: 009
Create Date: 2026-04-26
"""

from alembic import op
import sqlalchemy as sa


revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sub_topics",
        sa.Column(
            "difficulty",
            sa.Enum(
                "easy", "medium", "hard", name="sub_topics_difficulty"
            ),
            nullable=True,
            comment="Pedagogical difficulty — drives default task duration "
            "and ROI ranking.",
        ),
    )
    op.add_column(
        "sub_topics",
        sa.Column(
            "approximate_frequency",
            sa.Enum(
                "high", "medium", "low", name="sub_topics_frequency"
            ),
            nullable=True,
            comment="Test-frequency tier — drives ROI weighting.",
        ),
    )
    op.add_column(
        "sub_topics",
        sa.Column(
            "skill",
            sa.String(255),
            nullable=True,
            comment="College Board / AP skill label this sub_topic belongs to.",
        ),
    )
    op.add_column(
        "sub_topics",
        sa.Column(
            "domain",
            sa.String(255),
            nullable=True,
            comment="Top-level domain (e.g. Algebra, Geometry and Trig).",
        ),
    )


def downgrade() -> None:
    op.drop_column("sub_topics", "domain")
    op.drop_column("sub_topics", "skill")
    op.drop_column("sub_topics", "approximate_frequency")
    op.drop_column("sub_topics", "difficulty")
