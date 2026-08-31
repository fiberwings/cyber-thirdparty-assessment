"""Deterministic checks over attestation profiles (Phase 4, thin R2).

Pure functions — no LLM, no I/O — over (profile, analysis date, assessor
standards). Each finding carries the profile field's verbatim quote, so the
evidence trail is identical to a model-extracted weakness. Model-judged
staleness for SOC / ISO / pen-test documents is replaced by these checks
(the extraction prompts for those kinds no longer emit staleness findings).

Defaults (used only when the assessor standards profile does not state a
value) are conservative industry expectations, kept visible here:
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from app.schemas.attestation import AttestationProfileOut
from app.schemas.standards import StandardsProfile

# Attestations / audit periods older than this are stale (period end → analysis date).
DEFAULT_ATTESTATION_MAX_AGE_MONTHS = 12
# Penetration tests older than this are stale.
DEFAULT_PENTEST_MAX_AGE_MONTHS = 12
# A SOC Type 2 period shorter than this is a short examination window.
SHORT_PERIOD_MONTHS = 9
# Stale by more than twice the allowed age → high severity instead of medium.
SEVERE_STALE_FACTOR = 2.0


@dataclass
class CheckFinding:
    code: str            # stable identifier, also the dedupe discriminator
    kind: str            # "weakness" | "evidence_note"
    severity: str        # low|medium|high|critical (weaknesses only)
    description: str
    quotes: list[str] = field(default_factory=list)
    suggested_control_codes: list[str] = field(default_factory=list)


def _months_between(a: date, b: date) -> float:
    return (b.year - a.year) * 12 + (b.month - a.month) + (b.day - a.day) / 30.0


def _d(qd) -> date | None:
    return date.fromisoformat(qd.value) if qd is not None else None


def check_profile(
    profile: AttestationProfileOut,
    analysis_date: date,
    standards: StandardsProfile,
    *,
    doc_label: str,
) -> list[CheckFinding]:
    out: list[CheckFinding] = []
    max_age = standards.attestation_max_age_months or DEFAULT_ATTESTATION_MAX_AGE_MONTHS
    pentest_max_age = standards.pentest_max_age_months or DEFAULT_PENTEST_MAX_AGE_MONTHS

    is_soc = profile.doc_type.startswith("soc")
    period_start, period_end = _d(profile.period_start), _d(profile.period_end)

    # ---- SOC period checks ----
    if is_soc and period_start and period_end:
        months = _months_between(period_start, period_end)
        first = bool(profile.first_examination and profile.first_examination.value)
        if months < SHORT_PERIOD_MONTHS:
            quotes = [profile.period_start.quote, profile.period_end.quote]
            if first and profile.first_examination:
                quotes.append(profile.first_examination.quote)
                out.append(CheckFinding(
                    code="soc_short_first_examination",
                    kind="weakness",
                    severity="medium",
                    description=(
                        f"{doc_label}: the examination covers only ~{months:.0f} months "
                        f"({period_start} to {period_end}) and is the vendor's FIRST Type 2 "
                        f"examination, so no independent operating-effectiveness assurance "
                        f"exists for any earlier period."
                    ),
                    quotes=quotes,
                    suggested_control_codes=["AUDIT.SOC2"],
                ))
            else:
                out.append(CheckFinding(
                    code="soc_short_period",
                    kind="weakness",
                    severity="medium",
                    description=(
                        f"{doc_label}: the examination period covers only ~{months:.0f} months "
                        f"({period_start} to {period_end}), below the customary 12-month window."
                    ),
                    quotes=quotes,
                    suggested_control_codes=["AUDIT.SOC2"],
                ))
        elif first and profile.first_examination:
            out.append(CheckFinding(
                code="soc_first_examination",
                kind="evidence_note",
                severity="low",
                description=(
                    f"{doc_label}: first Type 2 examination — no earlier operating-effectiveness "
                    f"assurance exists."
                ),
                quotes=[profile.first_examination.quote],
            ))

    # ---- staleness (SOC period end / ISO expiry / pen-test date) ----
    ref = None
    ref_quote = None
    ref_what = ""
    if is_soc and period_end:
        ref, ref_quote, ref_what = period_end, profile.period_end.quote, "audit period end"
    elif profile.doc_type.startswith("iso") and profile.cert_expiry_date is not None:
        expiry = _d(profile.cert_expiry_date)
        if expiry < analysis_date:
            out.append(CheckFinding(
                code="iso_certificate_expired",
                kind="weakness",
                severity="high",
                description=(
                    f"{doc_label}: the ISO/IEC 27001 certificate expired on {expiry}, before the "
                    f"analysis date {analysis_date}."
                ),
                quotes=[profile.cert_expiry_date.quote],
                suggested_control_codes=["AUDIT.ISO"],
            ))
    elif profile.doc_type == "pentest":
        qd = profile.test_end_date or profile.test_start_date or profile.report_date
        if qd is not None:
            age = _months_between(date.fromisoformat(qd.value), analysis_date)
            if age > pentest_max_age:
                out.append(CheckFinding(
                    code="pentest_stale",
                    kind="weakness",
                    severity="high" if age > pentest_max_age * SEVERE_STALE_FACTOR else "medium",
                    description=(
                        f"{doc_label}: the most recent penetration test dates to {qd.value}, "
                        f"~{age:.0f} months before the analysis date {analysis_date} "
                        f"(allowed: {pentest_max_age})."
                    ),
                    quotes=[qd.quote],
                    suggested_control_codes=["VULN.PENTEST"],
                ))
    if ref is not None:
        age = _months_between(ref, analysis_date)
        if age > max_age:
            bridged = bool(profile.bridge_letter and profile.bridge_letter.value)
            quotes = [ref_quote]
            if bridged and profile.bridge_letter:
                quotes.append(profile.bridge_letter.quote)
            out.append(CheckFinding(
                code="attestation_stale",
                kind="evidence_note" if bridged else "weakness",
                severity="medium" if age <= max_age * SEVERE_STALE_FACTOR else "high",
                description=(
                    f"{doc_label}: the {ref_what} ({ref}) is ~{age:.0f} months before the analysis "
                    f"date {analysis_date} (allowed: {max_age} months)"
                    + ("; a bridge letter is referenced." if bridged else " and no bridge letter is referenced.")
                ),
                quotes=quotes,
                suggested_control_codes=["AUDIT.SOC2"],
            ))

    # ---- opinion ----
    if is_soc and profile.opinion is not None and profile.opinion.value.lower() in ("qualified", "adverse", "disclaimer"):
        out.append(CheckFinding(
            code=f"soc_opinion_{profile.opinion.value.lower()}",
            kind="evidence_note",  # the underlying exceptions are extracted as findings themselves
            severity="low",
            description=f"{doc_label}: the auditor's opinion is {profile.opinion.value}.",
            quotes=[profile.opinion.quote],
        ))
    return out


def check_required_attestations(
    profiles: dict[str, AttestationProfileOut],  # doc_label -> profile
    standards: StandardsProfile,
) -> list[CheckFinding]:
    """Assessment-level: each required attestation must be evidenced by some
    supplied assurance document (matched on the requirement wording vs doc
    type, deliberately coarse — 'SOC 2' matches any soc2_*, 'ISO' any iso*,
    'pen' any pentest)."""
    out: list[CheckFinding] = []
    have = {p.doc_type for p in profiles.values()}
    for req in standards.required_attestations:
        r = req.lower()
        ok = (
            ("soc" in r and any(t.startswith("soc") for t in have))
            or ("iso" in r and any(t.startswith("iso") for t in have))
            or (("pen" in r or "tlpt" in r) and "pentest" in have)
        )
        if not ok:
            out.append(CheckFinding(
                code=f"required_attestation_missing:{r[:40]}",
                kind="weakness",
                severity="high",
                description=(
                    f"The assessor requires \"{req}\" but no supplied assurance document "
                    f"evidences it (supplied: {', '.join(sorted(have)) or 'none'})."
                ),
                quotes=[],
                suggested_control_codes=["AUDIT.SOC2"],
            ))
    return out
