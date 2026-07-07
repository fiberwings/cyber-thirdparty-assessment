"""Benchmark case schema + YAML loader.

A case is a directory under cases/ containing case.yaml and a docs/ folder.
Golden expectations are hand-authored judgments — the answer key the app is
graded against.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DOC_KINDS = {"questionnaire", "soc", "iso", "pentest", "policy", "other"}
SEVERITIES = {"low", "medium", "high", "critical"}


class CaseDocument(BaseModel):
    path: str  # relative to the case directory
    kind: Literal["questionnaire", "soc", "iso", "pentest", "policy", "other"]


class ExpectedWeakness(BaseModel):
    id: str
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    mapped_control_codes: list[str] = Field(default_factory=list)
    # optional=true → nice-to-find: missing it is not a FN, matching it is not
    # a TP, and its matched actual is not a FP.
    optional: bool = False


class MustCoverPoint(BaseModel):
    id: str
    point: str


class MustNotClaim(BaseModel):
    id: str
    claim: str


class ExecSummaryRubric(BaseModel):
    must_cover: list[MustCoverPoint] = Field(default_factory=list)
    must_not_claim: list[MustNotClaim] = Field(default_factory=list)


class GoldenExpectations(BaseModel):
    expected_weaknesses: list[ExpectedWeakness] = Field(default_factory=list)
    exec_summary_rubric: ExecSummaryRubric = Field(default_factory=ExecSummaryRubric)

    @model_validator(mode="after")
    def unique_ids(self) -> "GoldenExpectations":
        ids = [w.id for w in self.expected_weaknesses]
        ids += [p.id for p in self.exec_summary_rubric.must_cover]
        ids += [c.id for c in self.exec_summary_rubric.must_not_claim]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate golden ids: {sorted(dupes)}")
        return self


class Case(BaseModel):
    id: str
    vendor_name: str
    description: str
    documents: list[CaseDocument]
    golden: GoldenExpectations
    # populated by the loader; not part of the YAML
    case_dir: Optional[Path] = None

    @field_validator("description")
    @classmethod
    def description_min_len(cls, v: str) -> str:
        if len(v.strip()) < 10:
            raise ValueError("description must be at least 10 characters (API minimum)")
        return v

    def doc_path(self, doc: CaseDocument) -> Path:
        assert self.case_dir is not None
        return self.case_dir / doc.path


class CaseLoadError(Exception):
    pass


def load_case(case_dir: Path) -> Case:
    yaml_path = case_dir / "case.yaml"
    if not yaml_path.exists():
        raise CaseLoadError(f"{yaml_path} not found")
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        case = Case.model_validate({**raw, "case_dir": case_dir})
    except Exception as e:  # pydantic or yaml errors — surface with path context
        raise CaseLoadError(f"{yaml_path}: {e}") from e
    missing = [str(d.path) for d in case.documents if not case.doc_path(d).exists()]
    if missing:
        raise CaseLoadError(f"{case.id}: missing document files: {missing}")
    return case


def load_cases(cases_dir: Path, only: list[str] | None = None) -> list[Case]:
    """Load all cases (or the named subset). Raises CaseLoadError on any invalid case."""
    if not cases_dir.exists():
        raise CaseLoadError(f"cases directory {cases_dir} not found")
    dirs = sorted(p for p in cases_dir.iterdir() if (p / "case.yaml").exists())
    if only:
        by_name = {p.name: p for p in dirs}
        unknown = [n for n in only if n not in by_name]
        if unknown:
            raise CaseLoadError(
                f"unknown cases: {unknown}; available: {sorted(by_name)}"
            )
        dirs = [by_name[n] for n in only]
    return [load_case(d) for d in dirs]
