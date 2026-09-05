"""Gmail IMAP — scan the last N hours of INBOX for recruiter/vendor requirement emails."""
from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup

from agent.models import Job
from agent.textsig import URL_RE, employment_hint, find_emails, find_rate, visa_hint, years_required_hint

from .base import Source

_SUBJECT_NOISE = re.compile(
    r"^\s*((re|fw|fwd|aw)\s*:\s*)+|"
    r"^\s*(urgent(ly)?\s*(requirement|need|hiring|opening)?|hot\s*(requirement|list)|immediate\s*(need|requirement|opening)|"
    r"new\s*(requirement|opening|job)|job\s*(opening|opportunity|title)|requirement|opening|position|role|hiring|"
    r"looking\s*for|need|c2c\s*(requirement|role|position)?|direct\s*client\s*(requirement)?)\s*[:\-–|]*\s*",
    re.I,
)
_FIELD_PATTERNS = {
    "title": r"(?:job\s*title|title|position|role)\s*[:\-–]\s*(.+)",
    "company": r"(?:end\s*client|client|company|customer|employer)\s*(?:name)?\s*[:\-–]\s*(.+)",
    "location": r"(?:job\s*)?location\s*[:\-–]\s*(.+)",
    "rate": r"(?:pay\s*rate|bill\s*rate|rate|pay|salary|compensation)\s*(?:\(.*?\))?\s*[:\-–]\s*(.+)",
    "duration": r"(?:duration|contract\s*length|term)\s*[:\-–]\s*(.+)",
    "employment": r"(?:employment\s*type|job\s*type|type|tax\s*terms?|engagement)\s*[:\-–]\s*(.+)",
}
_BAD_LINK = re.compile(r"(unsubscribe|optout|opt-out|mailtrack|list-manage|privacy|tracking|pixel|\.png|\.gif|\.jpg|linkedin\.com/in/|facebook\.com|twitter\.com|x\.com/|instagram\.com|google\.com/maps|calendly|zoom\.us)", re.I)


def _html_to_text(markup: str) -> str:
    soup = BeautifulSoup(markup or "", "html.parser")
    for t in soup(["script", "style", "head"]):
        t.decompose()
    return soup.get_text("\n", strip=True)


def _body_text(msg) -> str:
    plain, html_ = "", ""
    for part in msg.walk():
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        try:
            payload = part.get_content()
        except Exception:
            continue
        if not isinstance(payload, str):
            continue
        if ctype == "text/plain" and not plain:
            plain = payload
        elif ctype == "text/html" and not html_:
            html_ = payload
    text = plain if len(plain.strip()) > 80 else (_html_to_text(html_) if html_ else plain)
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _field(text: str, key: str) -> str:
    m = re.search(_FIELD_PATTERNS[key], text, re.I)
    if not m:
        return ""
    val = m.group(1).strip().strip("*_|").strip()
    val = re.split(r"\s{2,}|\s\|\s", val)[0]
    return val[:120]


def _clean_subject(subject: str) -> str:
    s = subject or ""
    for _ in range(3):
        s2 = _SUBJECT_NOISE.sub("", s).strip(" -–:|")
        if s2 == s:
            break
        s = s2
    return s[:140]


class GmailSource(Source):
    name = "gmail"

    def __init__(self, cfg, http):
        super().__init__(cfg, http)
        g = cfg.get("gmail", {}) or {}
        self.host = g.get("imap_host", "imap.gmail.com")
        self.folder = g.get("folder", "INBOX")
        self.lookback_hours = float(g.get("lookback_hours", 3))
        self.max_messages = int(g.get("max_messages", 200))
        self.ignore_domains = tuple(d.lower() for d in g.get("ignore_sender_domains", []))
        self.job_signals = [s.lower() for s in g.get("job_signals", [])]
        self.skill_signals = [s.lower() for s in g.get("skill_signals", [])] or [k.lower() for k in self.keywords]
        self.user = os.environ.get("GMAIL_USER", "")
        self.password = os.environ.get("GMAIL_APP_PASSWORD", "")

    # ----------------------------------------------------------------- helpers
    def _is_job_email(self, subject: str, body: str) -> bool:
        blob = f"{subject}\n{body}".lower()
        has_job = any(s in blob for s in self.job_signals) if self.job_signals else True
        has_skill = any(s in blob for s in self.skill_signals)
        return has_job and has_skill

    def _sender_ignored(self, addr: str) -> bool:
        dom = addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""
        return any(dom == d or dom.endswith("." + d) for d in self.ignore_domains)

    def _pick_url(self, text: str) -> str:
        for u in URL_RE.findall(text):
            u = u.rstrip(".,;:")
            if not _BAD_LINK.search(u):
                return u
        return ""

    def _parse_message(self, raw: bytes, cutoff: datetime) -> Job | None:
        msg = email.message_from_bytes(raw, policy=policy.default)
        try:
            sent = parsedate_to_datetime(msg.get("Date"))
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=timezone.utc)
        except Exception:
            sent = datetime.now(timezone.utc)
        if sent < cutoff:
            return None

        from_name, from_addr = parseaddr(msg.get("From", ""))
        reply_to = [a for _, a in getaddresses(msg.get_all("Reply-To", []) or []) if a]
        contact = (reply_to[0] if reply_to else from_addr or "").lower()
        if self._sender_ignored(from_addr) or (contact and self._sender_ignored(contact)):
            return None

        subject = str(msg.get("Subject", "")).replace("\n", " ").strip()
        body = _body_text(msg)
        if not self._is_job_email(subject, body):
            return None

        title = _field(body, "title") or _clean_subject(subject) or "Untitled requirement"
        company = _field(body, "company")
        if not company and from_name:
            # Vendor name from display name e.g. "Ravi | ABC Technologies"
            m = re.search(r"[|@\-–]\s*([A-Z][\w&.\s]{2,40})$", from_name)
            company = m.group(1).strip() if m else ""
        location = _field(body, "location")
        if not location:
            m = re.search(r"\b(remote|hybrid|onsite|on-site)\b[^\n]{0,40}", f"{subject}\n{body}", re.I)
            location = m.group(0).strip()[:80] if m else ""
        rate = _field(body, "rate") or find_rate(f"{subject}\n{body}")
        duration = _field(body, "duration")
        employment_line = _field(body, "employment")
        blob = f"{subject}\n{employment_line}\n{body}"

        # A vendor email counts as C2C-friendly context even if type is unclear; the model decides.
        emp_hint = employment_hint(blob) or "Vendor email (contract implied)"
        emails_in_body = [e for e in find_emails(body) if not self._sender_ignored(e)]
        if not contact and emails_in_body:
            contact = emails_in_body[0]

        desc_parts = [
            f"From: {from_name} <{from_addr}>",
            f"Reply-To: {', '.join(reply_to)}" if reply_to else "",
            f"Subject: {subject}",
            f"Duration: {duration}" if duration else "",
            f"Employment line: {employment_line}" if employment_line else "",
            "",
            body[:6000],
        ]
        return Job(
            title=title,
            company=company,
            location=location,
            rate=rate,
            description="\n".join(p for p in desc_parts if p is not None),
            url=self._pick_url(body),
            source=self.name,
            contact_email=contact,
            employment_hint=emp_hint,
            visa_hint=visa_hint(blob),
            years_hint=years_required_hint(body),
            posted_at=sent.isoformat(),
            extra={"message_id": msg.get("Message-ID", ""), "from": from_addr,
                   "other_emails": emails_in_body[:5]},
        ).clean()

    # ----------------------------------------------------------------- fetch
    def fetch(self) -> list[Job]:
        if not self.user or not self.password:
            raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD not set")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        # IMAP SINCE is date-granular; search from the cutoff's date (UTC-1 day for safety) then filter.
        since = (cutoff - timedelta(days=1)).strftime("%d-%b-%Y")

        conn = imaplib.IMAP4_SSL(self.host, 993, timeout=30)
        try:
            conn.login(self.user, self.password)
            status, _ = conn.select(self.folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"cannot select folder {self.folder}")
            status, data = conn.uid("search", None, f'(SINCE "{since}")')
            if status != "OK":
                raise RuntimeError("IMAP search failed")
            uids = data[0].split() if data and data[0] else []
            uids = uids[-self.max_messages:]
            self.log.info("scanning %d messages since %s (cutoff %s)", len(uids), since, cutoff.isoformat(timespec="minutes"))
            jobs: list[Job] = []
            for uid in reversed(uids):
                try:
                    status, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
                    if status != "OK" or not parts or not isinstance(parts[0], tuple):
                        continue
                    job = self._parse_message(parts[0][1], cutoff)
                    if job:
                        jobs.append(job)
                except Exception as exc:
                    self.log.warning("failed to parse uid=%s: %s", uid.decode() if isinstance(uid, bytes) else uid, exc)
            return jobs
        finally:
            try:
                conn.logout()
            except Exception:
                pass
