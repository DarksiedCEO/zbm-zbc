"""Homepage evidence uses the selected fetch and bounded parser sample, never another page."""
import copy

import pytest

import client_report as cr
import report_fixtures as fx
from agents import RunContext, entity_check
from primitives.fetch import FetchResult
from primitives.parse import parse_html

ORIGIN = 'https://owned.test'
PHONE = '202-555-0100'
ENTITY = {'entity_id': 'owned', 'version': 1, 'fields': {'telephone': {'value': PHONE}}}
FIELD = 'homepage_phone_observation'


def context(paths=('/',)):
    return RunContext(None, None, None, None, None, 'owned.test', paths=list(paths))


def page(text='', path='/', final=None):
    body = ('<html><body>' + text + '</body></html>').encode()
    final = ORIGIN + path if final is None else final
    return {'fetch': FetchResult(ORIGIN + path, 'OK', final_url=final, status=200,
                                 body=body, content_type='text/html'),
            'extract': parse_html(body.decode(), final)}


def result(pages, paths=('/',), entity=ENTITY):
    return entity_check.run(context(paths), pages, entity)


def observation(pages, **kw):
    return result(pages, **kw)['facts'][FIELD]


@pytest.mark.parametrize('home,contact,state', [(PHONE, '', 'found'), ('None here', PHONE, 'not_found_in_sample'),
                                               (PHONE, PHONE, 'found'), ('None', '', 'not_found_in_sample')])
def test_only_selected_homepage_counts(home, contact, state):
    out = result({'/': page(home), '/contact': page(contact, '/contact')}, ('/', '/contact'))
    assert out['facts'][FIELD]['state'] == state
    codes = {f['code'] for f in out['findings']}
    assert ('HOMEPAGE_PHONE_NOT_IN_TEXT_SAMPLE' in codes) == (state == 'not_found_in_sample')
    assert 'PHONE_NOT_IN_VISIBLE_TEXT' not in codes


@pytest.mark.parametrize('text,state', [(PHONE, 'found'), ('+1 (202) 555-0100', 'found'),
    ('2025550100', 'found'), ('ID 20255501009 suffix', 'not_found_in_sample'),
    ('ID 92025550100 suffix', 'not_found_in_sample'), ('x2025550100y', 'not_found_in_sample'),
    ('<script>2025550100</script>', 'not_found_in_sample'),
    ('<a href="tel:2025550100">Call</a>', 'not_found_in_sample'),
    ('<script type="application/ld+json">{"telephone":"2025550100"}</script>', 'not_found_in_sample')])
def test_complete_raw_text_phone_only(text, state):
    assert observation({'/': page(text)})['state'] == state


@pytest.mark.parametrize('prefix,suffix,state', [(19980, ' end', 'found'), (19995, ' end', 'not_found_in_sample'),
    (20001, ' end', 'not_found_in_sample'), (19989, '9 end', 'not_found_in_sample'),
    (19989, '', 'found')])
def test_actual_parser_cutoff(prefix, suffix, state):
    # Prefix has one observed separator; contiguous phone is ten characters.
    pg = page('x' * prefix + ' ' + '2025550100' + suffix)
    assert observation({'/': pg})['state'] == state


@pytest.mark.parametrize('final', [ORIGIN+'/contact', 'https://www.owned.test/', 'http://owned.test/',
                                 'https://other.test/', 'not-a-url'])
def test_final_url_never_promoted(final):
    assert observation({'/': page(PHONE, final=final)})['state'] == 'unknown'


@pytest.mark.parametrize('change', ['not_selected', 'missing', 'no_final', 'wrong_requested', 'timeout',
    'non_html', 'no_body', 'parse_error', 'missing_sample', 'bad_sample', 'bool_length', 'bad_length', 'no_parse_flag', 'bad_extract'])
def test_unread_or_inconsistent_evidence_is_unknown(change):
    pg = page(PHONE)
    paths, pages = ('/',), {'/': pg}
    if change == 'not_selected': paths = ('/contact',)
    elif change == 'missing': pages = {'/contact': page(PHONE, '/contact')}
    elif change == 'no_final': pg['fetch'].final_url = None
    elif change == 'wrong_requested': pg['fetch'].url = ORIGIN+'/contact'
    elif change == 'timeout': pg['fetch'].state = 'TIMEOUT'
    elif change == 'non_html': pg['fetch'].content_type = 'application/json'
    elif change == 'no_body': pg['fetch'].body = None
    elif change == 'parse_error': pg['extract']['parse_error'] = 'ValueError'
    elif change == 'missing_sample': pg['extract'].pop('text_sample')
    elif change == 'bad_sample': pg['extract']['text_sample'] = False
    elif change == 'bool_length': pg['extract']['text_length'] = True
    elif change == 'bad_length': pg['extract']['text_length'] += 1
    elif change == 'bad_extract': pg['extract'] = 'malformed'
    elif change == 'no_parse_flag': pg['extract'].pop('parse_error')
    out = result(pages, paths)
    assert out['facts'][FIELD]['state'] == 'unknown'
    assert out['outcome'] != 'OK'
    assert not any(f['code'] == 'HOMEPAGE_PHONE_NOT_IN_TEXT_SAMPLE' for f in out['findings'])


@pytest.mark.parametrize('entity', [None, {'entity_id':'owned','version':1,'fields':{}},
    {'entity_id':'owned','version':1,'fields':{'telephone':{'value':'123'}}}])
def test_no_canonical_phone_unknown(entity):
    assert observation({'/':page(PHONE)}, entity=entity)['state'] == 'unknown'


def view(obs, present=True):
    v = fx.fresh('full')
    env = next(e for e in v['report']['agents'] if e['agent'] == 'entity_check')
    env.setdefault('facts', {})
    if present:
        env['facts'][FIELD] = obs
        v['report']['domain'] = 'owned.test'
        v['domain'] = 'owned.test'
    return v


@pytest.mark.parametrize('fmt', ['html', 'markdown'])
@pytest.mark.parametrize('obs', [None, {}, {'schema':'future'}, {'schema':'homepage-phone-sample/1','reason':[]}, {'schema':'homepage-phone-sample/1','state':'found'}])
def test_present_malformed_never_legacy_clean(fmt, obs):
    data, model = cr.render(view(obs), 'acme', fmt)
    assert model['renderer_version'] == 'seo-client-report/5'
    assert 'Homepage phone sample: UNKNOWN' in data.decode()


@pytest.mark.parametrize('fmt', ['html', 'markdown'])
@pytest.mark.parametrize('text', [PHONE, 'None here'])
def test_new_observation_rendered_with_limits(fmt, text):
    obs = observation({'/':page(text)})
    data, model = cr.render(view(obs), 'acme', fmt)
    assert model['renderer_version'] == 'seo-client-report/5'
    assert 'sampled raw-html' in data.decode().lower() and '20000' in data.decode()
    label = 'FOUND' if text == PHONE else 'NOT FOUND IN SAMPLE'
    assert 'Homepage phone sample: ' + label in data.decode()
    if text == 'None here':
        assert text not in data.decode()


@pytest.mark.parametrize('fmt', ['html', 'markdown'])
def test_absent_field_keeps_legacy_bytes(fmt):
    v = fx.fresh('full')
    before = copy.deepcopy(v)
    a, model = cr.render(v, 'acme', fmt)
    assert model['renderer_version'] == 'seo-client-report/4'
    assert cr.render(view(None, present=False), 'acme', fmt)[0] == a
    assert v == before


@pytest.mark.local_http
@pytest.mark.parametrize('redirect', [False, True])
def test_real_fetch_chain_keeps_final_provenance_and_entity_check_fetches_nothing(srv, redirect):
    from agents import osei, selene
    from clock import FixedClock
    from fixture_server import install_site
    from helpers import T0
    install_site(srv)
    srv.html('site.test', '/', '<p>No telephone here.</p>')
    srv.html('site.test', '/contact', '<p>'+PHONE+'</p>')
    if redirect:
        srv.redirect('site.test', '/', 'http://site.test/contact')
    clock = FixedClock(T0)
    ctx = RunContext(srv.site_fetcher(), None, lambda **k: None, clock, osei.Osei(clock),
                     'site.test', scheme='http', paths=['/', '/contact'])
    _, pages = selene.run(ctx)
    seen = copy.deepcopy(srv.seen)
    out = entity_check.run(ctx, pages, ENTITY)
    obs = out['facts'][FIELD]
    assert obs['state'] == ('unknown' if redirect else 'not_found_in_sample')
    assert obs['requested_url'] == 'http://site.test/'
    assert obs['final_url'] == 'http://site.test/contact' if redirect else obs['final_url'] == 'http://site.test/'
    assert srv.seen == seen


@pytest.mark.parametrize('mutate', ['schema','state','reason','sizes','truncated','provenance'])
def test_corrupted_new_observation_is_unknown_not_legacy(mutate):
    obs = observation({'/':page(PHONE)})
    if mutate == 'schema': obs['schema'] = 'future'
    elif mutate == 'state': obs['state'] = 'delivered'
    elif mutate == 'reason': obs['reason'] = 'NOT_FOUND_IN_SAMPLE'
    elif mutate == 'sizes': obs['sample_chars'] = True
    elif mutate == 'truncated': obs['sample_truncated'] = 'false'
    elif mutate == 'provenance': obs['final_url'] = ORIGIN+'/contact'
    data, model = cr.render(view(obs), 'acme', 'html')
    assert model['renderer_version'] == 'seo-client-report/5'
    assert 'Homepage phone sample: UNKNOWN' in data.decode()
    sec = next(s for s in model['sections'] if s['key']=='entity')
    assert sec['status']['outcome'] != 'OK'
