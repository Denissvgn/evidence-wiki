"""A failing probe needs a named behavior assertion, not merely a failed process."""

from tools.probe_order_guards import behavioral_result


def test_named_failure_is_distinct_from_collection_error_and_unrelated_failure(tmp_path):
    xml = tmp_path / "result.xml"
    node = "tests/test_behavior.py::test_refuses"
    xml.write_text('<testsuite><testcase name="test_refuses"><failure>'
                   'AssertionError: 0 == 0</failure></testcase></testsuite>')
    assert behavioral_result(xml, [node], "0 == 0")["confirmed"]
    xml.write_text('<testsuite><testcase name="test_refuses"><error>'
                   'ImportError: dependency</error></testcase></testsuite>')
    assert not behavioral_result(xml, [node], "0 == 0")["confirmed"]
    xml.write_text('<testsuite><testcase name="test_something_else"><failure>'
                   'AssertionError: 0 == 0</failure></testcase></testsuite>')
    assert not behavioral_result(xml, [node], "0 == 0")["confirmed"]
