"""Assessor standards profile (R7).

The client organisation's own requirements — what it expects of a vendor
regardless of the vendor's evidence — so gaps that exist only relative to
those standards (e.g. "SOC 2 Type 2 required for PII processors", "policies
reviewed every 12 months") can be judged from a first-class input instead of
being smuggled into the service description.

Every field is optional. An empty profile means "no assessor standards
supplied": prompts then say so and the model applies typical industry
expectations, exactly as before this input existed.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class VulnSla(BaseModel):
    critical_days: Optional[int] = Field(default=None, ge=0)
    high_days: Optional[int] = Field(default=None, ge=0)
    medium_days: Optional[int] = Field(default=None, ge=0)


class StandardsProfile(BaseModel):
    # e.g. ["SOC 2 Type 2", "ISO/IEC 27001", "annual penetration test"]
    required_attestations: list[str] = Field(default_factory=list)
    # Maximum age of an attestation / audit period end before it is stale
    attestation_max_age_months: Optional[int] = Field(default=None, ge=1)
    # Maximum age of a penetration test report
    pentest_max_age_months: Optional[int] = Field(default=None, ge=1)
    # Expected policy review cadence
    policy_review_months: Optional[int] = Field(default=None, ge=1)
    # Regulatory record retention the vendor must support (years)
    retention_years: Optional[int] = Field(default=None, ge=0)
    # Regions / jurisdictions where data may be stored or processed
    allowed_residency: list[str] = Field(default_factory=list)
    # Free-text MFA expectation, e.g. "MFA on all accounts; phishing-resistant for privileged"
    mfa_policy: Optional[str] = None
    vuln_remediation_sla: Optional[VulnSla] = None
    # Anything else the assessor requires (one requirement per line/entry)
    other_requirements: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(
            [
                self.required_attestations,
                self.attestation_max_age_months,
                self.pentest_max_age_months,
                self.policy_review_months,
                self.retention_years,
                self.allowed_residency,
                self.mfa_policy,
                self.vuln_remediation_sla
                and any(
                    v is not None for v in self.vuln_remediation_sla.model_dump().values()
                ),
                self.other_requirements,
            ]
        )

    def render(self) -> str:
        """Prompt block. Deterministic, one line per stated requirement."""
        if self.is_empty():
            return (
                "(no assessor standards supplied — apply typical industry "
                "expectations for this kind of service)"
            )
        lines: list[str] = []
        if self.required_attestations:
            lines.append("- Required attestations: " + ", ".join(self.required_attestations))
        if self.attestation_max_age_months:
            lines.append(
                f"- Attestations / audit periods older than "
                f"{self.attestation_max_age_months} months are stale"
            )
        if self.pentest_max_age_months:
            lines.append(
                f"- Penetration test reports older than {self.pentest_max_age_months} months are stale"
            )
        if self.policy_review_months:
            lines.append(f"- Policies must be reviewed at least every {self.policy_review_months} months")
        if self.retention_years is not None:
            lines.append(f"- Regulated records must be retained for {self.retention_years} years")
        if self.allowed_residency:
            lines.append(
                "- Data may only be stored / processed in: " + ", ".join(self.allowed_residency)
            )
        if self.mfa_policy:
            lines.append(f"- MFA policy: {self.mfa_policy}")
        if self.vuln_remediation_sla:
            sla = self.vuln_remediation_sla
            bits = [
                f"{name} ≤ {days} days"
                for name, days in (
                    ("critical", sla.critical_days),
                    ("high", sla.high_days),
                    ("medium", sla.medium_days),
                )
                if days is not None
            ]
            if bits:
                lines.append("- Vulnerability remediation SLA: " + ", ".join(bits))
        for req in self.other_requirements:
            if req.strip():
                lines.append(f"- {req.strip()}")
        return "\n".join(lines)
