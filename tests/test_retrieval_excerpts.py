"""Exact whitespace, Unicode offsets and bounded incomplete context cases."""

import pytest

from evidence_wiki import retrieval_excerpts as module


@pytest.mark.parametrize('body,query', [
    ('Heading\n\nSpacing   is\tmeaningful.\r\nObserved ratio 3.', 'ratio'),
    ('A' * 2000 + '\n否定\nNot supported.', 'Not'),
    ('İstanbul\nStraße\nRatio 3.', 'ratio'),
    ('No matching passage here.', 'missing'),
], ids=['whitespace', 'long-body', 'unicode', 'no-match'])
def test_excerpt_is_exact_body_slice_not_collapsed_snippet(body, query):
    value = module.excerpt(body, query, width=128)
    assert value['text'] == body[value['start_char']:value['end_char']]
    assert len(value['text']) <= 128
    assert value['location_basis'] == 'exact_normalized_body'
    assert value['original_location'] == 'not_inferred'
    assert value['semantic_adequacy'] == 'not_evaluated'


def test_truncated_context_is_explicit_and_negation_is_retained():
    body = 'Start.\n' + 'x' * 3000 + '\nThis does not establish causation.\n' + 'y' * 3000
    value = module.excerpt(body, 'causation', width=128)
    assert 'does not establish causation' in value['text']
    assert value['prefix_omitted'] and value['suffix_omitted']
    assert value['start_line'] >= 2 and value['end_line'] >= value['start_line']


@pytest.mark.parametrize('width', [True, 0, 127, 4097])
def test_excerpt_refuses_unbounded_or_ambiguous_limits(width):
    with pytest.raises(ValueError, match='excerpt_input_bound'):
        module.excerpt('Exact context', 'context', width=width)
