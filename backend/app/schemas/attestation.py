"""Typed attestation profile (Phase 4, thin R2).

One small record per SOC / ISO / pen-test document, extracted with the fast
profile. Every populated field carries the verbatim quote it came from —
a field without a quote is treated as absent (floor: every profile field
carries a quote). Deterministic checks over these fields (see
app/attestation_checks.py) replace model-judged staleness for these
document kinds.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class QuotedStr(BaseModel):
    value: str
    quote: str = Field(min_length=4)


class QuotedDate(BaseModel):
    value: str  # ISO yyyy-mm-dd
    quote: str = Field(min_length=4)

    @model_validator(mode="after")
    def valid_date(self):
        from datetime import date

        date.fromisoformat(self.value)  # raises on garbage
        return self


class QuotedBool(BaseModel):
    """A True value must be quoted; False states an absence, which a document
    cannot be quoted for — so False may come without a quote."""

    value: bool
    quote: str = ""

    @model_validator(mode="after")
    def quote_required_when_true(self):
        if self.value and len(self.quote) < 4:
            raise ValueError("a True value requires the quote that states it")
        return self


class QuotedInt(BaseModel):
    value: int
    quote: str = Field(min_length=4)


class CarveOut(BaseModel):
    name: str
    service: str = ""
    quote: str = Field(min_length=4)


DocTypeLit = Literal[
    "soc1_type1", "soc1_type2", "soc2_type1", "soc2_type2", "soc3",
    "iso27001_certificate", "iso27001_audit", "pentest", "other",
]


class AttestationProfileOut(BaseModel):
    """Every field optional: absent = the document does not state it."""

    doc_type: DocTypeLit = "other"
    doc_type_quote: str = ""

    @field_validator("doc_type", mode="before")
    @classmethod
    def normalise_doc_type(cls, v):
        """Models write the type in many spellings ("SOC 2 Type II",
        "iso 27001 certificate"). Normalise to the canonical set; anything
        unrecognised becomes "other" (the checks then stay silent — an absent
        type never produces a wrong finding)."""
        if isinstance(v, dict):  # models often wrap it like the quoted fields
            v = v.get("value", "")
        if not isinstance(v, str):
            return "other"
        s = "".join(ch for ch in v.lower() if ch.isalnum() or ch == " ")
        s = s.replace("type ii", "type2").replace("type i", "type1").replace(" ", "")
        for canon in ("soc1_type1", "soc1_type2", "soc2_type1", "soc2_type2", "soc3",
                      "iso27001_certificate", "iso27001_audit", "pentest", "other"):
            if s == canon.replace("_", ""):
                return canon
        if "soc2type2" in s or ("soc2" in s and "type2" in s):
            return "soc2_type2"
        if "soc2type1" in s or ("soc2" in s and "type1" in s):
            return "soc2_type1"
        if "soc1type2" in s:
            return "soc1_type2"
        if "soc1type1" in s:
            return "soc1_type1"
        if "soc3" in s:
            return "soc3"
        if "iso" in s and ("cert" in s or "certificate" in s):
            return "iso27001_certificate"
        if "iso" in s:
            return "iso27001_audit"
        if "pen" in s and "test" in s or "pentest" in s:
            return "pentest"
        return "other"
    # SOC examination
    period_start: Optional[QuotedDate] = None
    period_end: Optional[QuotedDate] = None
    report_date: Optional[QuotedDate] = None
    first_examination: Optional[QuotedBool] = None
    opinion: Optional[QuotedStr] = None  # unqualified|qualified|adverse|disclaimer verbatim-backed
    carve_outs: list[CarveOut] = Field(default_factory=list)
    cuec_count: Optional[QuotedInt] = None
    auditor: Optional[QuotedStr] = None
    bridge_letter: Optional[QuotedBool] = None
    # ISO certificate / audit
    cert_issue_date: Optional[QuotedDate] = None
    cert_expiry_date: Optional[QuotedDate] = None
    certifying_body: Optional[QuotedStr] = None
    # Penetration test
    test_start_date: Optional[QuotedDate] = None
    test_end_date: Optional[QuotedDate] = None
    tester: Optional[QuotedStr] = None
    accreditation: Optional[QuotedStr] = None
    # Common
    scope: Optional[QuotedStr] = None
