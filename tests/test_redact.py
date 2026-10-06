from refund_desk.redact import Redactor, find_raw_pii, luhn_ok


def test_pii_becomes_placeholders_and_comes_back():
    r = Redactor()
    r.register_person("Maya Lindqvist", "maya.lindqvist@example.com")
    text = "I am Maya Lindqvist (maya.lindqvist@example.com), call +46 70 123 45 67. Maya"
    out = r.redact(text)
    assert "maya" not in out.lower() and "+46" not in out
    assert "<NAME_1>" in out and "<EMAIL_1>" in out and "<PHONE_1>" in out and "<FIRST_NAME_1>" in out
    assert r.rehydrate(out) == text


def test_secrets_are_removed_for_ever():
    r = Redactor()
    out = r.redact("card 4539 1488 0343 6467, key sk-ant-api03-abcdefghijk, password: hunter2")
    assert "[CARD_REMOVED]" in out and "[API_KEY_REMOVED]" in out and "[PASSWORD_REMOVED]" in out
    assert "4539" not in r.rehydrate(out)


def test_numbers_that_are_not_cards_stay():
    assert not luhn_ok("1234 5678 9012 3456")
    assert "1234 5678 9012 3456" in Redactor().redact("ref 1234 5678 9012 3456")
    assert "2026-10-06" in Redactor().redact("on 2026-10-06 order A-1001")


def test_first_name_match_is_case_sensitive():
    r = Redactor()
    r.register_person("Will Smith", "will@example.com")
    assert r.redact("I will call Will.") == "I will call <FIRST_NAME_1>."


def test_find_raw_pii():
    assert find_raw_pii("mail me at a.b@example.com") == ["EMAIL"]
    assert find_raw_pii("hello <EMAIL_1>") == []
