from pathlib import Path

import pytest

from bench.cases import CaseLoadError, load_case, load_cases

VALID_YAML = """\
id: demo
vendor_name: "Demo Vendor"
description: "A demo SaaS vendor processing PII in AWS us-east-1."
documents:
  - path: docs/policy.docx
    kind: policy
golden:
  expected_weaknesses:
    - id: D-W1
      description: "no mfa"
      severity: high
  exec_summary_rubric:
    must_cover:
      - id: D-K1
        point: "verdict stated"
    must_not_claim:
      - id: D-F1
        claim: "ISO certified"
"""


def _write_case(tmp_path: Path, yaml_text: str, with_doc: bool = True) -> Path:
    case_dir = tmp_path / "demo"
    (case_dir / "docs").mkdir(parents=True)
    (case_dir / "case.yaml").write_text(yaml_text)
    if with_doc:
        (case_dir / "docs" / "policy.docx").write_bytes(b"fake")
    return case_dir


def test_load_valid_case(tmp_path):
    case = load_case(_write_case(tmp_path, VALID_YAML))
    assert case.id == "demo"
    assert case.golden.expected_weaknesses[0].severity == "high"
    assert not case.golden.expected_weaknesses[0].optional
    assert case.doc_path(case.documents[0]).exists()


def test_missing_doc_file(tmp_path):
    _dir = _write_case(tmp_path, VALID_YAML, with_doc=False)
    with pytest.raises(CaseLoadError, match="missing document files"):
        load_case(_dir)


def test_bad_severity(tmp_path):
    bad = VALID_YAML.replace("severity: high", "severity: catastrophic")
    with pytest.raises(CaseLoadError):
        load_case(_write_case(tmp_path, bad))


def test_duplicate_golden_ids(tmp_path):
    bad = VALID_YAML.replace("id: D-K1", "id: D-W1")
    with pytest.raises(CaseLoadError, match="duplicate golden ids"):
        load_case(_write_case(tmp_path, bad))


def test_short_description(tmp_path):
    bad = VALID_YAML.replace(
        'description: "A demo SaaS vendor processing PII in AWS us-east-1."',
        'description: "short"',
    )
    with pytest.raises(CaseLoadError):
        load_case(_write_case(tmp_path, bad))


def test_load_cases_subset_and_unknown(tmp_path):
    _write_case(tmp_path, VALID_YAML)
    assert [c.id for c in load_cases(tmp_path, ["demo"])] == ["demo"]
    with pytest.raises(CaseLoadError, match="unknown cases"):
        load_cases(tmp_path, ["nope"])
