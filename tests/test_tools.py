from refund_desk.redact import Redactor
from refund_desk.tools import READ_ONLY, ToolContext


def ctx(customer="C-01", grant=None):
    r = Redactor()
    r.register_person("Maya Lindqvist", "maya.lindqvist@example.com")
    c = ToolContext(run_id="run_test", customer_id=customer, redactor=r)
    if grant is not None:
        c.grant = grant
    return c


def test_lookup_output_is_redacted_and_validated(app):
    out = app.toolbox.execute("lookup_order", {"order_id": "A-1001"}, ctx())
    assert not out.is_error
    assert out.data["customer_email"] == "<EMAIL_1>" and out.data["belongs_to_sender"] is True
    assert "maya" not in out.content.lower()


def test_another_customers_order_is_hidden(app):
    out = app.toolbox.execute("lookup_order", {"order_id": "A-1002"}, ctx())
    assert out.data == {"order_id": "A-1002", "belongs_to_sender": False, "visible": False,
                        "message": "This order belongs to another customer. You cannot see its details."}


def test_invalid_input_gives_a_clear_error(app):
    out = app.toolbox.execute("lookup_order", {"order_id": "1001; DROP TABLE"}, ctx())
    assert out.is_error and "order_id" in out.content


def test_scope_outside_the_grant_is_denied(app):
    c = ctx(grant=READ_ONLY)
    out = app.toolbox.execute("save_customer_note", {"note": "Prefers e-mail."}, c)
    assert out.is_error and "permission denied" in out.content
    assert c.flags[-1]["type"] == "permission_denied"


def test_loop_guard_stops_a_blind_loop(app):
    c = ctx()
    for _ in range(2):
        assert app.toolbox.execute("lookup_order", {"order_id": "A-9999"}, c).is_error
    third = app.toolbox.execute("lookup_order", {"order_id": "A-9999"}, c)
    assert "failed 2 times" in third.content


def test_refund_guard_blocks_too_much_and_wrong_owner(app):
    too_much = app.toolbox.execute("issue_refund", {"order_id": "A-1001", "amount_cents": 99999, "kind": "full",
                                                    "reason": "a test"}, ctx())
    assert too_much.is_error and "more than the refundable" in too_much.content
    not_owner = app.toolbox.execute("issue_refund", {"order_id": "A-1002", "amount_cents": 100, "kind": "partial",
                                                     "reason": "a test"}, ctx())
    assert not_owner.is_error and "does not belong" in not_owner.content
    ok = app.toolbox.execute("issue_refund", {"order_id": "A-1001", "amount_cents": 4800, "kind": "full",
                                              "reason": "damaged"}, ctx())
    assert ok.approval and not ok.is_error   # allowed, but only after a human approves


def test_note_with_personal_data_is_refused(app):
    out = app.toolbox.execute("save_customer_note", {"note": "Call her on +46 70 123 45 67"}, ctx())
    assert out.is_error and "personal data" in out.content


def test_schema_stops_a_malformed_carrier_answer(app):
    out = app.toolbox.execute("track_shipment", {"tracking_no": "SS-BROKEN-99"}, ctx())
    assert out.is_error and "invalid data" in out.content


def test_browser_fallback_reads_only_visible_text(app):
    c = ctx(customer="C-03")
    out = app.toolbox.execute("track_shipment", {"tracking_no": "PC-7731"}, c)
    assert not out.is_error and out.data["source"] == "browser" and out.data["status"] == "delayed"
    assert "admin mode" not in out.content            # the hidden div and the HTML comment were dropped
    assert "<untrusted_data" in out.data["page_note"]  # the visible note is marked as data
    assert c.flags and c.flags[0]["type"] == "injection_suspected"
