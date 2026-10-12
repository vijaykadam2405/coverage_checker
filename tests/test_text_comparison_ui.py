"""Saved text comparisons stay distinct from URL-presence and live extraction."""
import pytest

from crawler_coverage_auditor.analysis.states import text_comparison_state
from crawler_coverage_auditor.services import queries
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.validation.fingerprints import text_similarity
from test_react_api import make_inventory


def compared_row(**kwargs):
    return dict(page_type='HTML', validity='VALID_PAGE', match_status='EXACT_MATCH',
        text_length=100, corpus_text_length=80, similarity=.8, text_status='FOUND_MOSTLY_FULL') | kwargs


def test_reuses_saved_comparison_and_preserves_measured_zero():
    score = text_similarity('beginning middle ending', 'beginning middle')
    result = text_comparison_state(compared_row(similarity=score))
    assert result['text_coverage_percent'] == 66.7
    assert result['text_coverage_method'] == 'token_set_similarity'
    assert result['text_coverage_label'] == 'Mostly full'
    assert text_comparison_state(compared_row(similarity=0, corpus_text_length=0, text_status='TEXT_MISSING'))['text_coverage_percent'] == 0


@pytest.mark.parametrize('kwargs,label', [
    ({'provider':'coverage_api:https://api.test', 'text_status':'API_TEXT_NOT_SUPPORTED'}, 'Unavailable from Corpus API'),
    ({'text_status':'API_TEXT_NOT_SUPPORTED'}, 'Unavailable from Corpus API'),
    ({'page_type':'DOCUMENT','validity':'URL_ONLY'}, 'Not applicable · URL only'),
    ({'match_status':'NOT_FOUND'}, 'No corpus record to compare'),
    ({'match_status':None}, 'Awaiting corpus verification'),
    ({'validity':'HTTP_ERROR'}, 'Website text not validated'),
])
def test_unsupported_or_inapplicable_rows_never_invent_text_scores(kwargs,label):
    result=text_comparison_state(compared_row(**kwargs))
    assert result['text_coverage_percent'] is None and result['text_coverage_label']==label


@pytest.mark.parametrize('kwargs', [{'similarity':None},{'similarity':float('nan')},
    {'similarity':1.2},{'similarity':-.1},{'corpus_text_length':None},{'text_length':0}])
def test_incomplete_comparisons_do_not_show_percentages(kwargs):
    assert text_comparison_state(compared_row(**kwargs))['text_coverage_percent'] is None


def test_live_inventory_detail_and_export_include_same_comparison(tmp_path):
    path=tmp_path/'audit.sqlite';run=make_inventory(path)
    db=AuditDatabase(path)
    db.connection.execute("UPDATE http_results SET text_length=100 WHERE normalized_url='https://example.test/'")
    db.connection.execute("UPDATE corpus_matches SET similarity=.75,corpus_text_length=80,text_status='FOUND_PARTIAL_TEXT' WHERE normalized_url='https://example.test/'")
    db.connection.execute("UPDATE urls SET state='FETCHING' WHERE normalized_url='https://example.test/missing'")
    db.connection.commit();db.close()
    items=queries.url_page(path,run)['items']
    home=next(row for row in items if row['url']=='https://example.test/')
    assert home['text_coverage_percent']==75 and home['text_coverage_label']=='Partial content'
    assert queries.url_detail(path,run,home['id'])['detail']['Saved text similarity %']==75
    assert next(row for row in queries.live_urls(path,run) if row['id']==home['id'])['text_coverage_percent']==75
    assert queries.summary(path,run)['processing_urls'][0]['url']=='https://example.test/missing'
    exported=''.join(queries.export_csv(path,run,{}))
    assert 'Text similarity %' in exported and 'Partial content,75.0' in exported
