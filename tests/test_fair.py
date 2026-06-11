"""Tests for agentloom_runtime.fair."""

from __future__ import annotations

from agentloom_runtime.fair import (
    FAIRCalculator,
    calculate_fair_compliance,
    get_fair_percentage,
)


def test_fair_calculator_returns_result_for_minimal_metadata():
    metadata = {
        "datasetVersion": {
            "metadataBlocks": {
                "citation": {
                    "fields": [
                        {"typeName": "title", "value": "Example dataset"},
                        {"typeName": "author", "value": [{"authorName": "Example Author"}]},
                    ]
                }
            }
        },
        "persistentId": "doi:10.00000/example",
    }
    result = calculate_fair_compliance(metadata)
    assert 0 <= result.overall_score <= 100
    assert result.overall_status in {"compliant", "partial", "non-compliant"}


def test_get_fair_percentage_matches_calculator():
    metadata = {"datasetVersion": {"metadataBlocks": {}}}
    assert get_fair_percentage(metadata) == FAIRCalculator().calculate_compliance(metadata).overall_score
