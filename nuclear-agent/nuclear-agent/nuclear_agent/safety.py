"""Safety gate: decides whether an action may run, needs the user's OK, or is
something only the user may do.

Levels
  ok       - run it
  confirm  - pause the task and ask the user (purchases, messages, account
             changes, submitting personal info, deleting things ...)
  user     - the agent must never do this itself (passwords, payment card
             data, CAPTCHAs). The task pauses and the user does it in the
             browser window, then presses Resume.

The rules are deliberately cautious: a false "confirm" costs one click, a
missed one could cost money.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# English + Swedish words that signal an irreversible or outward-facing action.
SENSITIVE_CLICK_WORDS = [
    r"buy", r"buy now", r"purchase", r"checkout", r"check out", r"pay", r"pay now", r"place order",
    r"order now", r"complete order", r"confirm order", r"subscribe", r"send", r"post", r"publish",
    r"tweet", r"reply", r"comment", r"submit", r"delete", r"remove", r"confirm", r"transfer",
    r"donate", r"sign up", r"register", r"create account", r"unsubscribe", r"book now", r"book",
    r"reserve", r"apply", r"upload", r"save changes", r"change password", r"update account",
    r"close account", r"deactivate", r"accept", r"agree", r"authorize", r"authorise", r"allow",
    r"grant", r"install", r"download", r"share", r"invite", r"follow",
    # Swedish
    r"köp", r"köp nu", r"betala", r"beställ", r"slutför", r"skicka", r"publicera", r"radera",
    r"ta bort", r"bekräfta", r"registrera", r"skapa konto", r"prenumerera", r"boka", r"spara ändringar",
    r"godkänn", r"acceptera", r"överför", r"swisha", r"ladda ner", r"dela",
]
_CLICK_RE = re.compile(r"(?<![\w])(" + "|".join(sorted(SENSITIVE_CLICK_WORDS, key=len, reverse=True)) + r")(?![\w])",
                       re.IGNORECASE)

# Fields the agent must never fill.
SECRET_AUTOCOMPLETE = ("current-password", "new-password", "one-time-code", "cc-")
SECRET_NAME_RE = re.compile(
    r"pass(word|wd|code)?|pwd|pin\b|otp|2fa|cvc|cvv|csc|card.?num|cardnumber|cc.?num|credit|iban|"
    r"account.?num|routing|ssn|social.?security|personnummer|lösenord|kortnummer|bankid|secret|token|api.?key",
    re.IGNORECASE)

# Fields that hold personal data: typing into them needs confirmation.
PERSONAL_AUTOCOMPLETE = ("email", "tel", "name", "given-name", "family-name", "address", "street-address",
                         "postal-code", "bday", "sex", "country", "organization")
PERSONAL_TYPES = ("email", "tel")
PERSONAL_NAME_RE = re.compile(r"e-?mail|phone|tel|mobil|address|adress|postnummer|zip|birth|födelse|"
                              r"first.?name|last.?name|full.?name|förnamn|efternamn", re.IGNORECASE)
SEARCH_RE = re.compile(r"search|sök|query|find|\bq\b|filter", re.IGNORECASE)


@dataclass
class Verdict:
    level: str  # ok | confirm | user
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.level == "ok"


def _blob(el: dict) -> str:
    parts = (str(el.get(k, "") or "") for k in ("text", "label", "name", "placeholder"))
    return " ".join(p for p in parts if p).strip()


def is_secret_field(el: dict) -> bool:
    if (el.get("type") or "").lower() == "password":
        return True
    ac = (el.get("autocomplete") or "").lower()
    if any(ac.startswith(s) or s in ac for s in SECRET_AUTOCOMPLETE):
        return True
    return bool(SECRET_NAME_RE.search(" ".join(str(el.get(k, "")) for k in ("name", "label", "placeholder"))))


def is_personal_field(el: dict) -> bool:
    if (el.get("type") or "").lower() in PERSONAL_TYPES:
        return True
    ac = (el.get("autocomplete") or "").lower()
    if any(ac == a or ac.startswith(a) for a in PERSONAL_AUTOCOMPLETE):
        return True
    return bool(PERSONAL_NAME_RE.search(" ".join(str(el.get(k, "")) for k in ("name", "label", "placeholder"))))


def is_search_field(el: dict) -> bool:
    if (el.get("type") or "").lower() == "search" or (el.get("role") or "") == "searchbox":
        return True
    return bool(SEARCH_RE.search(" ".join(str(el.get(k, "")) for k in ("name", "label", "placeholder"))))


def assess(action: dict, elements: dict[int, dict], focused: dict | None = None) -> Verdict:
    """Judge one proposed action against the current element list."""
    kind = action.get("type")
    el = None
    if "element" in action:
        try:
            el = elements.get(int(action["element"]))
        except (TypeError, ValueError):
            el = None

    if kind in ("type", "select") and el is not None:
        if is_secret_field(el):
            return Verdict("user", "This field looks like a password, code or payment detail. "
                                   "Nuclear Agent never fills these in - please type it yourself in the browser, then press Resume.")
        if is_personal_field(el):
            return Verdict("confirm", f"Enter personal information into '{_blob(el)[:60]}'")
        if kind == "type" and action.get("submit") and not is_search_field(el):
            return Verdict("confirm", f"Type and submit a form via '{_blob(el)[:60]}'")
        return Verdict("ok")

    if kind == "click" and el is not None:
        text = _blob(el)
        m = _CLICK_RE.search(text)
        if m:
            return Verdict("confirm", f"Click '{text[:60]}' (looks like: {m.group(1).lower()})")
        if (el.get("type") or "").lower() == "submit" and el.get("in_form") and not SEARCH_RE.search(text):
            return Verdict("confirm", f"Submit a form via '{text[:60] or 'submit button'}'")
        return Verdict("ok")

    if kind == "click" and el is None:
        # The model may only click numbered elements; anything else is refused upstream.
        return Verdict("ok")

    if kind == "press" and str(action.get("key", "")) == "Enter" and focused:
        editable = focused.get("tag") in ("input", "textarea") or focused.get("editable")
        if editable and is_secret_field(focused):
            return Verdict("user", "Pressing Enter would submit a password/payment field. Please do this yourself.")
        if editable and not is_search_field(focused):
            return Verdict("confirm", f"Press Enter in '{_blob(focused)[:60] or focused.get('tag')}' (this may send or submit it)")

    return Verdict("ok")
