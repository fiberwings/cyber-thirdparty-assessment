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

        # Phase 5: one critical earns +1 uplift, but residual is capped at
        # inherent unless the deficiency is auditor-tested. This row has no
        # kind_signal → vendor_admitted → capped at inherent (2). Counted
        # twice (old bug) nothing changes either — the point of bounded uplift.
        (score,) = scores
        assert score.uplift == 1 and score.distinct_high_critical == 1
        assert score.residual_likelihood == 2


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

        # Phase 5: two distinct highs earn +1 (never 2), capped at inherent
        # for vendor-only evidence → residual stays 1. If wrongly deduped to
        # one high, uplift would be 0 — distinctness still observable.
        (score,) = scores
        assert score.uplift == 1 and score.distinct_high_critical == 2
        assert score.residual_likelihood == 1


def test_soc_exception_kind_lifts_residual_above_inherent(fresh_db):
    """auditor-tested evidence (kind_signal soc_exception) gates the ceiling."""
    with SessionLocal() as db:
        a = _make_assessment(db, inherent_likelihood=2)
        db.add(Weakness(
            assessment_id=a.id, severity="critical", description="SOC exception: MFA absent",
            unmatched=False, mapped_control_codes=["IAM.MFA"], dedupe_key="w-soc",
            kind_signal="soc_exception"))
        db.commit()
        db.refresh(a)
        (score,), agg = _recalculate_in_session(db, a)
        assert score.residual_likelihood == 3  # inherent + 1
        assert score.auditor_tested_high_critical == 1
        assert agg.confidence == "high"
