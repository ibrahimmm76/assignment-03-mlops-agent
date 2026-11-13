from agent.grounding import find_unsupported_values

EVIDENCE = [{"metrics": {"f1": 0.9861, "recall": 0.9861}, "run_id": "b40fb22a6e9c4a6c8f73fa0a88d590b8",
             "commit": "8cd0a69b05ec37cd02869fbd6e53f008f457aec5", "pods": 2}]


def test_values_from_evidence_are_supported_in_any_format():
    answer = "Run b40fb22a (commit 8cd0a69) has F1 0.986, i.e. 98.6%, with 2 pods."
    assert find_unsupported_values(answer, EVIDENCE) == []


def test_invented_values_are_detected():
    answer = "F1 is 0.9734, accuracy 97%, trained at commit deadbee1."
    assert find_unsupported_values(answer, EVIDENCE) == ["0.9734", "97%", "deadbee1"]
