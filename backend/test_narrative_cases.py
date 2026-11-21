from test_phase1 import client
from test_phase2 import sealed
from narrative_cases import cases
from reports import collect_packet
from report_content import validate_document

def test_thirty_labeled_narrative_cases(client,sealed):
    packet=collect_packet('demo',sealed)
    corpus=cases(packet)
    assert len(corpus)>=30
    for case in corpus:
        result=validate_document(case['document'],packet)
        assert result['valid']==case['expected_valid'],(case['label'],result)
