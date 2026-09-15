import unittest

from spellguard.analysis import parse_snapshot
from spellguard.models import CollectedSnapshot, Coverage, Snapshot, SourceFile
from spellguard.rules.nested_literal_case import detect_nested_literal_cases
from spellguard.rules.repeated_decision import detect_repeated_decisions
from spellguard.rules.exception_return import detect_exception_returns


def findings_for(source):
    collected = CollectedSnapshot(
        Snapshot(
            "snapshot",
            "working-tree",
            (SourceFile("sample.py", source, "digest"),),
        ),
        Coverage(1, 1, 0, 0, 0, (), True),
    )
    result = parse_snapshot(collected)
    return result, detect_exception_returns(result.facts)


class ExceptionReturnRuleTest(unittest.TestCase):
    def test_three_literal_exception_returns_are_candidates(self):
        _, findings = findings_for(
            """def zero():
    try:
        work()
    except Exception:
        return 0

def empty():
    try:
        work()
    except Exception:
        return []

def conditional():
    try:
        work()
    except Exception:
        if fallback:
            return "fallback"
"""
        )

        self.assertEqual(len(findings), 3)
        self.assertEqual([item.rule_id for item in findings], ["SG001"] * 3)
        self.assertEqual([item.severity for item in findings], ["medium"] * 3)
        self.assertEqual(len({item.fingerprint for item in findings}), 3)

    def test_raise_call_and_normal_return_are_not_candidates(self):
        _, findings = findings_for(
            """def raise_only():
    try:
        work()
    except Exception:
        raise

def repair_result():
    try:
        work()
    except Exception:
        return repair()

def normal_default():
    try:
        work()
    except Exception:
        raise
    return 0
"""
        )

        self.assertEqual(findings, ())

    def test_first_raise_and_finally_boundary_are_reported_conservatively(self):
        _, findings = findings_for(
            """def boundaries():
    try:
        work()
    except Exception:
        raise
        return 0

    try:
        work()
    except Exception:
        record()
    finally:
        return 1

    try:
        work()
    except Exception:
        def inner():
            return 2
        class Nested:
            def method(self):
                return []
"""
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].primary_location.start_line, 6)

    def test_whitespace_and_comments_keep_identity_but_literal_change_does_not(self):
        _, original = findings_for(
            """def sample():
    try:
        work()
    except Exception:
        return 0
"""
        )
        _, reformatted = findings_for(
            """def sample():
    # same structure
    try:
        work()
    except Exception:
        return 0
"""
        )
        _, changed = findings_for(
            """def sample():
    try:
        work()
    except Exception:
        return 1
"""
        )

        self.assertEqual(original[0].fingerprint, reformatted[0].fingerprint)
        self.assertNotEqual(original[0].fingerprint, changed[0].fingerprint)


class RepeatedDecisionRuleTest(unittest.TestCase):
    def test_two_complete_chains_in_different_functions_produce_one_group(self):
        result, _ = findings_for(
            """def first(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2
    return 0

def second(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2
    return 0
"""
        )

        findings = detect_repeated_decisions(result.facts)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "SG002")
        self.assertEqual(findings[0].primary_location.start_line, 2)
        self.assertEqual(len(findings[0].related_locations), 1)
        self.assertEqual(findings[0].related_locations[0].start_line, 9)

    def test_comments_and_layout_do_not_change_repeated_chain_identity(self):
        first, _ = findings_for(
            """def first(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2

def second(value):
    # same decision, documented differently
    if value == \"a\":
        return 1
    elif (value == \"b\"):
        return 2
"""
        )
        second, _ = findings_for(
            """def first(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2

def second(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2
"""
        )

        first_finding = detect_repeated_decisions(first.facts)[0]
        second_finding = detect_repeated_decisions(second.facts)[0]

        self.assertEqual(first_finding.fingerprint, second_finding.fingerprint)

    def test_three_arm_chain_with_final_else_is_grouped(self):
        result, _ = findings_for(
            """def first(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2
    elif value == \"c\":
        return 3
    else:
        return 0

def second(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2
    elif value == \"c\":
        return 3
    else:
        return 0
"""
        )

        findings = detect_repeated_decisions(result.facts)

        self.assertEqual(len(findings), 1)
        self.assertEqual(len(findings[0].related_locations), 1)

    def test_single_guards_different_conditions_or_bodies_do_not_match(self):
        result, _ = findings_for(
            """def single(value):
    if value == \"a\":
        return 1

def different_condition(value):
    if value == \"x\":
        return 1
    elif value == \"b\":
        return 2

def different_body(value):
    if value == \"a\":
        return 9
    elif value == \"b\":
        return 2
"""
        )

        self.assertEqual(detect_repeated_decisions(result.facts), ())

    def test_else_if_is_not_treated_as_an_elif_chain(self):
        result, _ = findings_for(
            """def first(value):
    if value:
        return 1
    else:
        if value == \"b\":
            return 2

def second(value):
    if value:
        return 1
    else:
        if value == \"b\":
            return 2
"""
        )

        self.assertEqual(detect_repeated_decisions(result.facts), ())


class NestedLiteralCaseRuleTest(unittest.TestCase):
    def test_three_nested_literal_cases_are_candidates(self):
        result, _ = findings_for(
            """def customer_case(customer, version):
    if customer == \"acme\":
        if version == 2:
            return \"legacy\"

def region_case(region, mode):
    if region == \"eu\":
        if mode == \"safe\":
            return True

def compound_case(customer, region, version):
    if customer == \"acme\" and region == \"us\":
        if version == 3:
            return False
"""
        )

        findings = detect_nested_literal_cases(result.facts)

        self.assertEqual(len(findings), 3)
        self.assertEqual([item.rule_id for item in findings], ["SG003"] * 3)
        self.assertEqual(findings[0].primary_location.start_line, 2)
        self.assertEqual(findings[0].related_locations[0].start_line, 3)

    def test_flat_or_invalid_conditions_and_indirect_nesting_do_not_match(self):
        result, _ = findings_for(
            """def flat(value):
    if value == \"a\":
        return 1
    elif value == \"b\":
        return 2

def range_case(value):
    if value >= 1:
        if value == 2:
            return 2

def none_case(value):
    if value == None:
        if value == \"x\":
            return 1

def or_case(value):
    if value == \"a\" or value == \"b\":
        if value == \"c\":
            return 1

def loop_case(values):
    if values == \"special\":
        for value in values:
            if value == \"nested\":
                return value

def function_case(value):
    if value == \"outer\":
        def inner():
            if value == \"inner\":
                return value
        return inner()
"""
        )

        self.assertEqual(detect_nested_literal_cases(result.facts), ())

    def test_comments_and_positions_do_not_change_nested_case_fingerprint(self):
        first, _ = findings_for(
            """def sample(value):
    if value == \"outer\":
        # same structure
        if value == \"inner\":
            return 1
"""
        )
        second, _ = findings_for(
            """def sample(value):
    if value == \"outer\":


        if value == \"inner\":
            return 1
"""
        )

        first_finding = detect_nested_literal_cases(first.facts)[0]
        second_finding = detect_nested_literal_cases(second.facts)[0]

        self.assertEqual(first_finding.fingerprint, second_finding.fingerprint)


if __name__ == "__main__":
    unittest.main()
