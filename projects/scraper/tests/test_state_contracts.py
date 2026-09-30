"""How the state file and the committed contracts combine in `load_state`.

The contract (`specs/<id>/expected.json`) says a human approved the source; the
state says what runs have since found out. Verified comes from the first,
quarantine from the second -- and a re-onboarding, which only ever changes the
first, has to be able to lift a quarantine the cluster recorded, because nobody
edits the cluster's state file.
"""

from __future__ import annotations

from themis_scraper import registry

_CONTRACT = {"page_type": "topics", "verified_content_sha1": "aaa"}
_REFROZEN = {"page_type": "topics", "verified_content_sha1": "bbb"}


def _reconciled(entry: dict, contract: dict | None) -> dict:
    registry._reconcile_with_contract(entry, contract)
    return entry


def test_a_contract_verifies_an_unverified_source():
    entry = _reconciled({"onboarding": registry.ONBOARD_UNVERIFIED}, _CONTRACT)
    assert entry["onboarding"] == registry.ONBOARD_VERIFIED
    assert entry["page_type"] == "topics"


def test_no_contract_leaves_a_source_unverified():
    entry = _reconciled({"onboarding": registry.ONBOARD_UNVERIFIED}, None)
    assert entry == {"onboarding": registry.ONBOARD_UNVERIFIED}


def test_the_contract_page_type_wins_over_a_stale_state_copy():
    entry = _reconciled(
        {"onboarding": registry.ONBOARD_VERIFIED, "page_type": "process"}, _CONTRACT
    )
    assert entry["page_type"] == "topics"


def test_a_quarantine_of_the_current_contract_holds():
    entry = _reconciled(
        {"onboarding": registry.ONBOARD_QUARANTINED, "quarantined_contract": "aaa"}, _CONTRACT
    )
    assert entry["onboarding"] == registry.ONBOARD_QUARANTINED


def test_a_re_onboarding_lifts_the_quarantine():
    entry = _reconciled(
        {"onboarding": registry.ONBOARD_QUARANTINED, "quarantined_contract": "aaa"}, _REFROZEN
    )
    assert entry["onboarding"] == registry.ONBOARD_VERIFIED
    assert "quarantined_contract" not in entry


def test_a_quarantine_without_a_recorded_contract_holds():
    # Recorded before the field existed: nothing to tell a re-onboarding apart by.
    entry = _reconciled({"onboarding": registry.ONBOARD_QUARANTINED}, _REFROZEN)
    assert entry["onboarding"] == registry.ONBOARD_QUARANTINED


def test_contract_id_of_a_contract_without_a_hash_is_stable():
    assert registry.contract_id({"page_type": "topics"}) == ""
    assert registry.contract_id(None) == ""
