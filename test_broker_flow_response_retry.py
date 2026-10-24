"""A malformed ZP-style response gets one bounded read, without weaker admission."""
from test_neobdm_source_contract import FlowPage, capture, flow_body, flow_response, AKUM, ns as source


class ResponseSequencePage(FlowPage):
    def __init__(self, sequences):
        super().__init__({code: values[0] for code, values in sequences.items()})
        self.sequences = {code:list(values) for code, values in sequences.items()}
        self.waits = []

    def click(self, selector, timeout=None):
        if selector == '#submit-button':
            values = self.sequences[self.code]
            self.responses[self.code] = values.pop(0) if len(values)>1 else values[0]
        return super().click(selector, timeout)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)
        return super().wait_for_timeout(milliseconds)


def malformed(code):
    body = flow_body()
    body['response'][AKUM]['children'] = None
    return flow_response(code, body)


def test_fresh_valid_response_recovers_and_keeps_both_side_contracts(monkeypatch):
    page = ResponseSequencePage({'ZP':[malformed('ZP'),flow_response('ZP')]})
    result, _ = capture(monkeypatch, {}, codes=('ZP',), page=page)
    assert result['reason'] is None
    assert result['scans'][0]['status'] == source.FLOW_OK
    assert result['scans'][0]['response_retry']['attempts'] == 2
    assert '.children is not a list' in result['scans'][0]['response_retry']['first_failure']
    assert page.submits == 2
    assert source.FLOW_RESPONSE_RETRY_DELAY_SECONDS*1000 in page.waits
    assert result['rows'] and all(row['broker_code']=='ZP' for row in result['rows'])


def test_persistent_shape_failure_is_still_refused(monkeypatch):
    page = ResponseSequencePage({'ZP':[malformed('ZP')]})
    result, _ = capture(monkeypatch, {}, codes=('ZP',), page=page)
    assert result['reason'] and result['rows'] == []
    assert result['scans'][0]['status'] == source.FLOW_SOURCE_FAILURE
    assert page.submits == 2


def test_whole_sweep_has_only_one_extra_submit(monkeypatch):
    page = ResponseSequencePage({'XL':[malformed('XL'),flow_response('XL')],
                                 'ZP':[malformed('ZP')]})
    result, _ = capture(monkeypatch, {}, codes=('XL','ZP'), page=page)
    assert page.submits == 3
    assert result['scans'][0]['status'] == source.FLOW_OK
    assert result['scans'][1]['status'] == source.FLOW_SOURCE_FAILURE
    assert result['reason']


def test_provider_pause_with_http_200_is_not_retried(monkeypatch):
    body = flow_body()
    body['response'][AKUM]['children'] = 'Abnormal usage detected. Please try later.'
    page = ResponseSequencePage({'ZP':[flow_response('ZP',body)]})
    result, _ = capture(monkeypatch, {}, codes=('ZP',), page=page)
    assert result['reason'] and result['rows'] == [] and page.submits == 1
    assert 'source paused' in result['scans'][0]['detail']


def test_http_throttle_is_not_retried(monkeypatch):
    response = flow_response('ZP')
    response.status = 429
    page = ResponseSequencePage({'ZP':[response]})
    result, _ = capture(monkeypatch, {}, codes=('ZP',), page=page)
    assert result['reason'] and page.submits == 1


def test_a_missing_date_label_cannot_be_retried_into_admission(monkeypatch):
    body = flow_body(akum_label=None)
    page = ResponseSequencePage({'ZP':[flow_response('ZP',body),flow_response('ZP')]})
    result, _ = capture(monkeypatch, {}, codes=('ZP',), page=page)
    assert result['reason'] and result['rows'] == [] and page.submits == 1


def test_healthy_response_keeps_one_submit(monkeypatch):
    page = ResponseSequencePage({'ZP':[flow_response('ZP')]})
    result, _ = capture(monkeypatch, {}, codes=('ZP',), page=page)
    assert result['reason'] is None and page.submits == 1
    assert 'response_retry' not in result['scans'][0]
