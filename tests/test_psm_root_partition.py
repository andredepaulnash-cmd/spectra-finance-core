"""Pure binding tests for later PSM handoffs of actual bridge-verified root cash.

The envelopes here exercise ownership binding only. Actual native/allocator DB
source verification is covered by test_psm_root_funding and caller cycle tests.
"""
from copy import deepcopy
import pytest
import test_psm_profit_partition as F
from research.weekly_reversal import psm_root_partition as P


@pytest.fixture
def native_source(tmp_path,monkeypatch,request):
    monkeypatch.setattr(F,'P',P)
    return F.native_source.__wrapped__(tmp_path,monkeypatch,request)


def root_lineage(native_source,retained):
    fixture,evidence=F.dedicated_source(native_source)
    state,source,at=fixture;buy=state['orders']['buy'];meta=state['psm']['BTC-USD']
    owner={'schema':1,'pair':'BTC-USD','root_cohort_id':'root','funding_owner':'seed',
           'source_pool':'psm:BTC-USD','latest_generation':1,'ownership_proof_sha256':'9'*64}
    buy.update(source_pool='psm:BTC-USD',source_owner=owner)
    if retained:
        buy['pool']='psm:BTC-USD'
        for records in (meta['original_lineage'],meta['episode_lineage']):
            for row in records:row['source_pool']=buy['pool']
        meta['campaign_lineage_sha256']=F.digest({k:meta[k] for k in P.LINEAGE_FIELDS})
        base={'schema':1,'order_id':'buy','entry_id':'buy','source_pool':buy['pool'],'entry_pool':buy['pool'],
              'source_owner':deepcopy(owner),'base_entry':{'approved_profit_increment':'0'},
              'evidence':{'order':deepcopy(buy),'root_source':{'proof_sha256':'9'*64}}}
        base['proof_sha256']=F.digest(base);buy['base_entry_proof']=base
        buy['activation']['proof_sha256']=base['proof_sha256']
        evidence['activation_sha256']=base['proof_sha256'];evidence['entry_pool']=buy['pool']
        evidence['lineage']={key:evidence['lineage'][key] for key in
            ('schema','order_id','pair','cohort','cohort_generation','funding_owner')}
        evidence['lineage'].update(kind='RETAINED_PSM_ROOT_ENTRY',entry_pool=buy['pool'],
            base_entry_proof_sha256=base['proof_sha256'],root_source_sha256='9'*64,source_owner=deepcopy(owner))
    else:
        evidence['lineage'].update(source_pool=buy['source_pool'],psm_root_source=deepcopy(owner))
    F.rehash_funding(evidence)
    proof=F.verify_funding(fixture,evidence)
    source['order']=deepcopy(state['orders']['sell']);source['psm_ownership']=[proof]
    for acquisition in source['acquisitions']:
        acquisition['order']=deepcopy(buy);acquisition['pool_transition']=deepcopy(proof)
    return fixture,evidence


@pytest.mark.parametrize('retained',[False,True])
def test_exact_funded_or_retained_psm_root_handoff_replays(native_source,retained):
    fixture,evidence=root_lineage(native_source,retained)
    original=deepcopy(fixture[0]);proof=F.verify_funding(fixture,evidence)
    updated=F.stage(fixture)
    assert fixture[0]==original
    assert F.verify_funding((updated,fixture[1],fixture[2]),evidence)==proof
    assert F.stage((updated,fixture[1],fixture[2]))==updated


@pytest.mark.parametrize('retained',[False,True])
@pytest.mark.parametrize('key,value',[
    ('schema',True),('pair','ETH-USD'),('root_cohort_id','foreign'),('funding_owner','repair'),
    ('source_pool','seed'),('latest_generation',2),('ownership_proof_sha256','bad')])
def test_resigned_root_owner_cannot_cross_scope(native_source,retained,key,value):
    fixture,evidence=root_lineage(native_source,retained)
    owner=evidence['lineage']['source_owner' if retained else 'psm_root_source']
    owner[key]=value;F.rehash_funding(evidence)
    with pytest.raises(P.Blocked):F.verify_funding(fixture,evidence)


@pytest.mark.parametrize('retained',[False,True])
@pytest.mark.parametrize('field',['source_owner','activation','source_pool'])
def test_original_root_acquisition_proof_is_mandatory(native_source,retained,field):
    fixture,evidence=root_lineage(native_source,retained)
    fixture[0]['orders']['buy'].pop(field)
    with pytest.raises(P.Blocked):F.verify_funding(fixture,evidence)


@pytest.mark.parametrize('field',['base_entry_proof_sha256','root_source_sha256'])
def test_retained_root_proof_digest_cannot_be_substituted(native_source,field):
    fixture,evidence=root_lineage(native_source,True)
    evidence['lineage'][field]='8'*64;F.rehash_funding(evidence)
    with pytest.raises(P.Blocked):F.verify_funding(fixture,evidence)
