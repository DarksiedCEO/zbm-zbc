"""Real portal, existing in-memory Legal fixture; no claim of durable Legal proof."""
from helpers import TOKEN, ORIGIN, MSA_TEXT, SOW_TEXT, call, form_post, make, sha, view
from helpers import tok
from legal_client import Document


def committed():
    app, legal, clock, mono = make()
    page = call(app, 'GET', f'/accept/{TOKEN}')
    assert form_post(app,get=page).status == 200
    assert len(legal.used_by[TOKEN][1].acceptances) == 2
    return app, legal, page


def check(response, legal, status):
    assert response.status == status
    assert len(legal.used_by[TOKEN][1].acceptances) == 2
    assert 'Nothing was recorded' not in response.text
    assert 'An earlier submission may already have been recorded' in response.text


def test_not_now_after_committed_submission():
    app,legal,page=committed()
    response=call(app,'GET','/accept/not-now')
    check(response,legal,200)
    assert 'This page does not submit an acceptance' in response.text
    assert len(legal.accepts)==1


def test_oversized_retry_after_committed_submission():
    app,legal,page=committed()
    response=call(app,'POST',f'/accept/{TOKEN}',{'content-type':'application/x-www-form-urlencoded','origin':ORIGIN,'content-length':'10000'},b'x'*10000)
    check(response,legal,413)
    assert 'This attempt was refused because the form was too large' in response.text
    assert len(legal.accepts)==1


def test_changed_other_open_link_does_not_deny_prior_acceptance():
    # A prior agreement is recorded; a second open link changes before submission.
    # This is not a claim that a used token returns409 (its normal path returns404/receipt).
    app,legal,page=committed()
    second=tok('another-open-link')
    legal.views[second]=view()
    opened=call(app,'GET',f'/accept/{second}')
    changed=SOW_TEXT+' Revised scope.'
    legal.views[second]=view(docs=[Document('client_msa','1.0',sha(MSA_TEXT),MSA_TEXT),Document('sow_seo_audit','1.2',sha(changed),changed)])
    response=form_post(app,token=second,get=opened)
    check(response,legal,409)
    assert 'This attempt was refused' in response.text
    assert len(legal.accepts)==1
