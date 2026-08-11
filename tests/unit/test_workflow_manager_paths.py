from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models import WorkflowType
from app.services import workflow_manager as wm
from app.services.workflow_manager import PendingFlag, WorkflowManager, WorkflowStage


FIXED_NOW = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


@pytest.fixture
def session():
    """A persistence-free stand-in for ConversationSession."""
    return SimpleNamespace(
        session_id="session-1",
        workflow_type=None,
        workflow_state={},
    )


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(wm, "utc_now", lambda: FIXED_NOW)
    return FIXED_NOW


@pytest.mark.unit
def test_workflow_type_conversion_transition_and_validation(session):
    assert WorkflowManager.get_workflow_type(session) is None
    assert WorkflowManager.set_workflow_type(session, WorkflowType.authentication, caller="test")
    assert WorkflowManager.get_workflow_type(session) is WorkflowType.authentication

    assert WorkflowManager.set_workflow_type(session, "registration", caller="test")
    assert session.workflow_type is WorkflowType.registration
    assert not WorkflowManager.set_workflow_type(session, "not-a-workflow", caller="test")
    assert session.workflow_type is WorkflowType.registration

    session.workflow_type = "not-a-workflow"
    assert WorkflowManager.get_workflow_type(session) is None
    session.workflow_type = None
    assert WorkflowManager.can_transition(session, WorkflowType.user_exit)

    session.workflow_type = WorkflowType.authentication
    assert WorkflowManager.can_transition(session, WorkflowType.registration)
    assert not WorkflowManager.can_transition(session, WorkflowType.user_exit)
    session.workflow_type = WorkflowType.buy_something
    assert not WorkflowManager.can_transition(session, WorkflowType.registration)

    session.workflow_type = WorkflowType.authentication
    assert WorkflowManager.transition_workflow(
        session,
        WorkflowType.registration,
        WorkflowStage.CONFIRMING,
        caller="test",
    )
    assert session.workflow_type is WorkflowType.registration
    assert session.workflow_state["stage"] == "confirming"

    # Invalid transitions are logged but deliberately allowed for compatibility.
    assert WorkflowManager.transition_workflow(
        session, WorkflowType.rfq_creation, validate=True, caller="test"
    )
    assert session.workflow_type is WorkflowType.rfq_creation
    assert WorkflowManager.transition_workflow(
        session, WorkflowType.user_exit, validate=True, caller="test"
    )
    assert session.workflow_type is WorkflowType.user_exit
    assert WorkflowManager.transition_workflow(
        session, "invalid-without-validation", validate=False, caller="test"
    )
    assert session.workflow_type is WorkflowType.user_exit


@pytest.mark.unit
def test_stage_and_pending_flags_cover_valid_invalid_and_group_helpers(session):
    assert WorkflowManager.get_stage(session) is None
    WorkflowManager.set_stage(session, WorkflowStage.COLLECTING, caller="test")
    assert WorkflowManager.get_stage(session) is WorkflowStage.COLLECTING
    WorkflowManager.set_stage(session, "collecting", caller="test")
    assert WorkflowManager.get_stage(session) is WorkflowStage.COLLECTING
    session.workflow_state["stage"] = "bad-stage"
    assert WorkflowManager.get_stage(session) is None
    session.workflow_state = {"other": True}
    assert WorkflowManager.get_stage(session) is None
    empty_pending = SimpleNamespace(session_id="empty-pending", workflow_type=None, workflow_state={})
    assert WorkflowManager.get_pending_value(empty_pending, PendingFlag.RFQ) is None
    assert WorkflowManager.get_pending_value(session, PendingFlag.RFQ) is None

    WorkflowManager.set_pending(session, PendingFlag.RFQ, {"id": 1}, caller="test")
    WorkflowManager.set_pending(session, PendingFlag.OPTIONAL_RFQ, True, caller="test")
    WorkflowManager.set_pending(session, PendingFlag.OPTIONAL_COMBINED_RFQ, False, caller="test")
    WorkflowManager.set_pending(session, "pending_rfq", caller="test")
    assert WorkflowManager.has_pending(session, PendingFlag.RFQ)
    assert WorkflowManager.get_pending_value(session, PendingFlag.RFQ) == {"id": 1}
    assert not WorkflowManager.has_pending(session, PendingFlag.OPTIONAL_COMBINED_RFQ)
    assert WorkflowManager.has_any_pending_confirmation(session)
    assert WorkflowManager.has_any_optional_confirmation(session)
    assert not WorkflowManager.has_pending(session, "pending_rfq")
    assert WorkflowManager.get_pending_value(session, "pending_rfq") is None

    WorkflowManager.clear_pending(
        session, PendingFlag.RFQ, "pending_optional_rfq", caller="test"
    )
    WorkflowManager.clear_pending(session, PendingFlag.ACCOUNT_CHANGE, caller="test")
    assert WorkflowManager.has_any_pending_confirmation(session)
    WorkflowManager.clear_pending(session, PendingFlag.OPTIONAL_RFQ, caller="test")
    assert not WorkflowManager.has_any_pending_confirmation(session)
    WorkflowManager.clear_pending(session, caller="test")
    empty = SimpleNamespace(session_id="empty", workflow_state={})
    WorkflowManager.clear_pending(empty, PendingFlag.RFQ, caller="test")
    assert not WorkflowManager.has_pending(empty, PendingFlag.RFQ)

    for flag in (
        PendingFlag.RFQ,
        PendingFlag.COMBINED_RFQ,
        PendingFlag.OPTIONAL_RFQ,
        PendingFlag.OPTIONAL_COMBINED_RFQ,
        PendingFlag.ROLE_SWITCH,
        PendingFlag.ACCOUNT_SWITCH,
        PendingFlag.INTENT_SWITCH,
        PendingFlag.AUTH_REG_SWITCH,
        PendingFlag.ATTACHMENT_DECISION,
    ):
        WorkflowManager.set_pending(session, flag, True, caller="test")
    WorkflowManager.clear_all_rfq_pending(session, caller="test")
    assert not WorkflowManager.has_any_pending_confirmation(session)
    WorkflowManager.clear_all_decision_pending(session, caller="test")
    assert not any(
        WorkflowManager.has_pending(session, flag)
        for flag in (
            PendingFlag.ROLE_SWITCH,
            PendingFlag.ACCOUNT_SWITCH,
            PendingFlag.INTENT_SWITCH,
            PendingFlag.AUTH_REG_SWITCH,
            PendingFlag.ATTACHMENT_DECISION,
        )
    )


@pytest.mark.unit
def test_initialize_and_safe_reset_preserve_expected_fields(session, fixed_clock):
    blank = SimpleNamespace(session_id="blank", workflow_type=None, workflow_state={})
    WorkflowManager.safe_reset_workflow_state(blank, caller="test")
    assert blank.workflow_state == {
        "stage": "collecting",
        "last_activity_at": fixed_clock.isoformat(),
    }

    WorkflowManager.initialize_workflow_state(session)
    assert session.workflow_state == {
        "extracted_entities": [],
        "stage": "collecting",
        "last_activity_at": fixed_clock.isoformat(),
    }

    session.workflow_state["session_archive"] = {"old": True}
    session.workflow_state["custom"] = "kept"
    WorkflowManager.initialize_workflow_state(session)
    assert "session_archive" not in session.workflow_state
    assert session.workflow_state["custom"] == "kept"
    WorkflowManager.initialize_workflow_state(session)

    partial = SimpleNamespace(
        session_id="partial", workflow_type=None,
        workflow_state={"extracted_entities": ["only-this"]},
    )
    WorkflowManager.safe_reset_workflow_state(partial, caller="test")
    assert partial.workflow_state["extracted_entities"] == ["only-this"]

    session.workflow_state = {
        "extracted_entities": ["entity"],
        "complete_products": ["product"],
        "conversation_history": ["message"],
        "custom": "preserve-me",
        "pending_optional_rfq": {"data": 1},
        "discard": True,
    }
    WorkflowManager.safe_reset_workflow_state(
        session, preserve_fields=["custom"], caller="test"
    )
    assert session.workflow_state == {
        "stage": "collecting",
        "last_activity_at": fixed_clock.isoformat(),
        "extracted_entities": ["entity"],
        "complete_products": ["product"],
        "conversation_history": ["message"],
        "custom": "preserve-me",
    }


@pytest.mark.unit
def test_track2_delivery_extraction_modification_and_context(session):
    assert WorkflowManager.get_delivery_details(session) is None
    assert not WorkflowManager.is_delivery_confirmed(session)
    assert not WorkflowManager.is_initial_extraction_complete(session)
    assert not WorkflowManager.can_proceed_to_items(session)
    assert WorkflowManager.get_track2_context(session) == {}

    details = {"city": "Pune", "pincode": "411005"}
    WorkflowManager.set_delivery_details(session, details, caller="test")
    assert WorkflowManager.get_delivery_details(session) == details
    assert not WorkflowManager.is_delivery_confirmed(session)
    WorkflowManager.confirm_delivery(session, caller="test")
    assert WorkflowManager.is_delivery_confirmed(session)
    assert WorkflowManager.can_proceed_to_items(session)
    assert session.workflow_state["awaiting_delivery_modification"] is False

    WorkflowManager.mark_initial_extraction_complete(session, caller="test")
    assert WorkflowManager.is_initial_extraction_complete(session)

    WorkflowManager.set_awaiting_modification(session, "delivery", "delivery text", caller="test")
    assert WorkflowManager.is_awaiting_modification(session) == (True, "delivery")
    assert session.workflow_state["format_retry_count"] == 0
    WorkflowManager.set_awaiting_modification(session, "items", "items text", caller="test")
    # Both flags can be present; delivery is intentionally checked first.
    assert WorkflowManager.is_awaiting_modification(session) == (True, "delivery")
    WorkflowManager.clear_awaiting_modification(session, caller="test")
    WorkflowManager.set_awaiting_modification(session, "items", "items text", caller="test")
    assert WorkflowManager.is_awaiting_modification(session) == (True, "items")
    WorkflowManager.set_awaiting_modification(session, "unknown", "ignored", caller="test")
    assert session.workflow_state["format_modification_subtype"] == "items"

    assert WorkflowManager.increment_retry_count(session, caller="test") == 1
    assert WorkflowManager.increment_retry_count(session, caller="test") == 2
    assert WorkflowManager.get_retry_count(session) == 2
    WorkflowManager.reset_retry_count(session, caller="test")
    assert WorkflowManager.get_retry_count(session) == 0
    WorkflowManager.clear_awaiting_modification(session, caller="test")
    assert WorkflowManager.is_awaiting_modification(session) == (False, None)
    assert session.workflow_state["original_format"] is None

    context = WorkflowManager.get_track2_context(session)
    assert context["delivery_confirmed"] is True
    assert context["delivery_details"] == details
    assert context["initial_extraction_complete"] is True
    assert context["can_proceed_to_items"] is True

    session.workflow_state["format_retry_count"] = "bad"
    with pytest.raises(TypeError):
        WorkflowManager.increment_retry_count(session, caller="test")


@pytest.mark.unit
def test_interruption_context_and_all_resume_prompt_branches(session):
    assert not WorkflowManager.can_resume_workflow(session)
    assert WorkflowManager.get_resume_prompt(session) is None

    session.workflow_state = {
        "delivery_confirmed": False,
        "delivery_details": {"city": "Pune"},
        "awaiting_items_modification": True,
        "extracted_entities": [{"name": "bolt"}],
        "format_retry_count": 2,
    }
    WorkflowManager.save_interruption_context(session, "faq", caller="test")
    assert WorkflowManager.can_resume_workflow(session)
    assert session.workflow_state["resume_context"] == {
        "interrupted_by": "faq",
        "delivery_confirmed": False,
        "awaiting_modification": True,
        "modification_subtype": "items",
        "has_delivery_details": True,
        "has_extracted_entities": True,
        "retry_count": 2,
    }
    assert "modifying your items" in WorkflowManager.get_resume_prompt(session)

    prompt_cases = [
        (
            {"has_extracted_entities": True},
            "reviewing your items",
        ),
        (
            {"has_delivery_details": True, "delivery_confirmed": False},
            "confirming your delivery details",
        ),
        (
            {"delivery_confirmed": True},
            "share the items you need",
        ),
        ({}, "What would you like to do next?"),
    ]
    for context, expected in prompt_cases:
        session.workflow_state["resume_context"] = context
        assert expected in WorkflowManager.get_resume_prompt(session)

    assert WorkflowManager.resume_workflow(session, caller="test") == {}
    assert session.workflow_state["can_resume"] is False
    assert session.workflow_state["interrupted_by"] is None

    fresh = SimpleNamespace(session_id="fresh", workflow_type=None, workflow_state=None)
    assert WorkflowManager.resume_workflow(fresh, caller="test") == {}
    assert fresh.workflow_state == {
        "interrupted_by": None,
        "can_resume": False,
    }
    interrupted = SimpleNamespace(session_id="interrupted", workflow_type=None, workflow_state=None)
    WorkflowManager.save_interruption_context(interrupted, "greeting", caller="test")
    assert interrupted.workflow_state["can_resume"] is True


@pytest.mark.unit
def test_sectioned_rfq_lifecycle_and_unknown_sections(session):
    assert not WorkflowManager.is_sectioned_rfq_active(session)
    session.workflow_state = {"other": True}
    assert not WorkflowManager.is_sectioned_rfq_active(session)
    session.workflow_state = {}
    assert WorkflowManager.get_sectioned_rfq_section(session) is None
    assert not WorkflowManager.is_section_confirmed(session, "items")
    assert WorkflowManager.get_section_data(session, "items") is None
    assert not WorkflowManager.is_awaiting_section_modification(session, "items")
    assert not WorkflowManager.is_sectioned_rfq_pending_restart(session)
    assert not WorkflowManager.is_sectioned_rfq_from_excel(session)

    WorkflowManager.initialize_sectioned_rfq(session, caller="test")
    state = session.workflow_state["sectioned_rfq"]
    assert state["active"] is False
    assert set(state["sections"]) == {
        "date_location", "items", "attachments", "final_confirmation"
    }

    WorkflowManager.set_sectioned_rfq_section(session, "items", caller="test")
    assert WorkflowManager.get_sectioned_rfq_section(session) == "items"
    WorkflowManager.confirm_sectioned_section(session, "items", caller="test")
    assert WorkflowManager.is_section_confirmed(session, "items")
    WorkflowManager.update_section_data(session, "items", [{"name": "bolt"}], caller="test")
    assert WorkflowManager.get_section_data(session, "items") == [{"name": "bolt"}]
    assert WorkflowManager.increment_section_retry(session, "items", caller="test") == 1
    WorkflowManager.reset_section_retry(session, "items", caller="test")
    assert state["sections"]["items"]["retry_count"] == 0

    WorkflowManager.set_awaiting_section_modification(session, "items", True, caller="test")
    assert WorkflowManager.is_awaiting_section_modification(session, "items")
    WorkflowManager.set_sectioned_rfq_pending_restart(session, True, caller="test")
    assert WorkflowManager.is_sectioned_rfq_pending_restart(session)
    WorkflowManager.set_sectioned_rfq_from_excel(session, caller="test")
    assert WorkflowManager.is_sectioned_rfq_from_excel(session)

    # Unknown sections are intentionally ignored by section mutators/readers.
    WorkflowManager.set_sectioned_rfq_section(session, "unknown", caller="test")
    WorkflowManager.confirm_sectioned_section(session, "unknown", caller="test")
    WorkflowManager.update_section_data(session, "unknown", "ignored", caller="test")
    assert WorkflowManager.increment_section_retry(session, "unknown", caller="test") == 0
    WorkflowManager.reset_section_retry(session, "unknown", caller="test")
    WorkflowManager.set_awaiting_section_modification(session, "unknown", True, caller="test")
    assert WorkflowManager.get_section_data(session, "unknown") is None
    assert not WorkflowManager.is_section_confirmed(session, "unknown")
    assert not WorkflowManager.is_awaiting_section_modification(session, "unknown")

    WorkflowManager.reset_sectioned_rfq(session, caller="test")
    assert WorkflowManager.get_sectioned_rfq_section(session) == "date_location"
    assert not WorkflowManager.is_sectioned_rfq_pending_restart(session)
    assert not WorkflowManager.is_sectioned_rfq_from_excel(session)

    no_nested_state = SimpleNamespace(session_id="no-nested", workflow_type=None, workflow_state={})
    WorkflowManager.reset_section_retry(no_nested_state, "items", caller="test")
    assert no_nested_state.workflow_state == {}


@pytest.mark.unit
def test_sectioned_mutators_auto_initialize_and_empty_readers(session):
    WorkflowManager.set_sectioned_rfq_section(session, "date_location", caller="test")
    assert session.workflow_state["sectioned_rfq"]["current_section"] == "date_location"

    new_session = SimpleNamespace(session_id="new", workflow_type=None, workflow_state=None)
    WorkflowManager.update_section_data(new_session, "date_location", {"city": "Pune"}, caller="test")
    assert WorkflowManager.get_section_data(new_session, "date_location") == {"city": "Pune"}
    confirm_session = SimpleNamespace(session_id="confirm", workflow_type=None, workflow_state={})
    WorkflowManager.confirm_sectioned_section(confirm_session, "date_location", caller="test")
    assert WorkflowManager.is_section_confirmed(confirm_session, "date_location")
    WorkflowManager.confirm_sectioned_section(new_session, "date_location", caller="test")
    assert WorkflowManager.is_section_confirmed(new_session, "date_location")

    another = SimpleNamespace(session_id="another", workflow_type=None, workflow_state=None)
    assert WorkflowManager.increment_section_retry(another, "items", caller="test") == 1
    assert another.workflow_state["sectioned_rfq"]["sections"]["items"]["retry_count"] == 1

    third = SimpleNamespace(session_id="third", workflow_type=None, workflow_state=None)
    WorkflowManager.set_awaiting_section_modification(third, "items", True, caller="test")
    assert WorkflowManager.is_awaiting_section_modification(third, "items")

    fourth = SimpleNamespace(session_id="fourth", workflow_type=None, workflow_state=None)
    WorkflowManager.set_sectioned_rfq_pending_restart(fourth, True, caller="test")
    WorkflowManager.set_sectioned_rfq_from_excel(fourth, False, caller="test")
    assert WorkflowManager.is_sectioned_rfq_pending_restart(fourth)
    assert not WorkflowManager.is_sectioned_rfq_from_excel(fourth)
    fifth = SimpleNamespace(session_id="fifth", workflow_type=None, workflow_state={})
    WorkflowManager.set_sectioned_rfq_from_excel(fifth, caller="test")
    assert WorkflowManager.is_sectioned_rfq_from_excel(fifth)
