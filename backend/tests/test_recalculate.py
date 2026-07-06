"""Recalc-layer tests for weakness → scenario bucketing and dedup.

Regression guard for the uplift dedup bug: the old code keyed the per-scenario
`seen` set on `id()` of an ephemeral tuple, which never dedupes (and can
spuriously collide), so a weakness mapped to several of one scenario's
controls was double-counted toward its uplift.

Inherent likelihoods are chosen low enough that the residual doesn't clamp at
4, so a single-band difference in uplift stays observable.
"""

from __future__ import annotations

from app.api.scoring import _recalculate_in_session
from app.db import SessionLocal
from app.models import Assessment, ExpectedControl, Scenario, Weakness


def _make_assessment(db, *, inherent_likelihood: int) -> Assessment:
    a = Assessment(vendor_name="Acme")
    db.add(a)
    db.flush()
    s = Scenario(
        assessment_id=a.id,
        code="DATA_LEAK",
        name="PII leakage",
        description="x",
        inherent_impact=3,
        inherent_likelihood=inherent_likelihood,
    )
    db.add(s)
    db.flush()
    db.add(ExpectedControl(scenario_id=s.id, code="IAM.MFA", name="MFA"))
    db.add(ExpectedControl(scenario_id=s.id, code="ENC.REST", name="Encryption at rest"))
    db.flush()
    return a


def test_weakness_mapped_to_two_controls_counts_once(fresh_db):
    with SessionLocal() as db:
        a = _make_assessment(db, inherent_likelihood=2)
        db.add(
            Weakness(
                assessment_id=a.id,
                severity="critical",
                description="Shared admin credentials, no MFA, data unencrypted",
                unmatched=False,
                mapped_control_codes=["IAM.MFA", "ENC.REST"],
                dedupe_key="w-both",
            )
        )
        db.commit()
        db.refresh(a)

        scores, _ = _recalculate_in_session(db, a)

        # Unassessed controls contribute no likelihood reduction. One critical
        # weakness = 1.0 raw uplift → 1 band. Double-counted (old bug) it would
        # be 2.0 raw → 2 bands → residual 4.
        (score,) = scores
        assert score.residual_likelihood == 2 + 1


def test_two_identical_shape_weaknesses_count_twice(fresh_db):
    """Two distinct weaknesses with identical severity + codes must both count
    (guards against the opposite failure: spurious dedupe of different rows)."""
    with SessionLocal() as db:
        a = _make_assessment(db, inherent_likelihood=1)
        for i in range(2):
            db.add(
                Weakness(
                    assessment_id=a.id,
                    severity="high",
                    description=f"finding {i}",
                    unmatched=False,
                    mapped_control_codes=["IAM.MFA"],
                    dedupe_key=f"w-{i}",
                )
            )
        db.commit()
        db.refresh(a)

        scores, _ = _recalculate_in_session(db, a)

        # high = 0.75 raw each → 1.5 combined → 2 bands. If wrongly deduped to
        # one weakness it would be 0.75 → 1 band.
        (score,) = scores
        assert score.residual_likelihood == 1 + 2
