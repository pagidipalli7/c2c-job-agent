"""Fast offline tests: run with  .venv/bin/python -m pytest -q  (or python -m unittest)."""
import unittest

from agent.analyzer import ClaudeAnalyzer
from agent.dedupe import Deduper
from agent.models import Job, make_job_id, normalize_url
from agent.textsig import employment_hint, find_rate, visa_hint
from sources.dice import parse_search_page


class TestJobId(unittest.TestCase):
    def test_url_normalization_strips_tracking_and_www(self):
        a = normalize_url("https://www.dice.com/job-detail/ABC?utm_source=x&rx_src=y#frag")
        b = normalize_url("https://dice.com/job-detail/ABC/")
        self.assertEqual(a, b)

    def test_hash_fallback_is_case_insensitive(self):
        x = make_job_id("", "ACME", "Data Engineer", "Remote")
        y = make_job_id("", "acme", "data engineer", "remote")
        self.assertEqual(x, y)
        self.assertEqual(len(x), 40)


class TestDedupe(unittest.TestCase):
    def test_sheet_and_within_run(self):
        j1 = Job(title="A", company="C", location="Remote", url="https://x.com/1")
        j2 = Job(title="A", company="C", location="Remote", url="https://www.x.com/1?utm_source=z")
        j3 = Job(title="B", company="C", location="Remote")
        d = Deduper({j3.job_id})
        self.assertEqual([j.title for j in d.filter_new([j1, j2, j3])], ["A"])


class TestHeuristics(unittest.TestCase):
    def test_employment(self):
        self.assertEqual(employment_hint("Corp to Corp only, no W2"), "C2C")
        self.assertEqual(employment_hint("W2 contract, 12 months"), "W2")
        self.assertEqual(employment_hint("Full-time permanent role"), "Full-time")

    def test_visa(self):
        self.assertEqual(visa_hint("Only USC/GC candidates"), "Restricted")
        self.assertEqual(visa_hint("We cannot provide sponsorship"), "Restricted")
        self.assertEqual(visa_hint("H1B transfer welcome"), "H1B-OK")
        self.assertEqual(visa_hint("Great team, Azure Databricks"), "")

    def test_rate(self):
        self.assertEqual(find_rate("Rate: $65/hr on C2C"), "$65/hr")
        self.assertIn("60", find_rate("paying $60 - $70 per hour"))


class TestAnalyzerParsing(unittest.TestCase):
    def test_strict_json(self):
        out = ClaudeAnalyzer.parse_response('{"jobs":[{"index":0,"match_percent":85}]}')
        self.assertEqual(out[0]["index"], 0)

    def test_fenced_and_prose(self):
        text = 'Here you go:\n```json\n{"jobs":[{"index":1,"match_percent":"90%","visa_status":"h1b ok"}]}\n```'
        out = ClaudeAnalyzer.parse_response(text)
        c = ClaudeAnalyzer._coerce(out[0])
        self.assertEqual((c["match_percent"], c["visa_status"]), (90, "H1B-OK"))

    def test_truncated_salvage(self):
        text = '{"jobs":[{"index":0,"match_percent":80,"missing_skills":"","employment_type":"C2C","visa_status":"Unclear","contact_email":""},{"index":1,"match_pe'
        out = ClaudeAnalyzer.parse_response(text)
        self.assertEqual(len(out), 1)


class TestDiceParser(unittest.TestCase):
    def test_parse_rsc_payload(self):
        inner = ('12:["$","$L32",null,{"jobList":{"data":[{"guid":"g1","title":"Data Engineer",'
                 '"companyName":"ACME","detailsPageUrl":"https://www.dice.com/job-detail/g1",'
                 '"jobLocation":{"displayName":"Remote"}}],"meta":{"totalResults":1,"pageSize":30}}}]')
        page = '<script>self.__next_f.push([1,"' + inner.replace('"', '\\"') + '"])</script>'
        jobs, meta = parse_search_page(page)
        self.assertEqual(jobs[0]["title"], "Data Engineer")
        self.assertEqual(meta["totalResults"], 1)


if __name__ == "__main__":
    unittest.main()


class _FakeUsage:
    input_tokens = 10
    output_tokens = 5


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]
        self.stop_reason = "end_turn"
        self.model = "fake"
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        import json as _json
        payload = _json.loads(kwargs["messages"][0]["content"].split("<jobs>")[1].split("</jobs>")[0])
        out = []
        for j in payload:
            out.append({
                "index": j["index"],
                "match_percent": 92 if "Databricks" in j["title"] else 40,
                "missing_skills": "" if "Databricks" in j["title"] else "Snowflake, Java",
                "employment_type": "C2C" if j["employment_hint"] == "C2C" else "Unclear",
                "visa_status": "H1B-OK" if j["employment_hint"] == "C2C" else "Unclear",
                "contact_email": j["contact_email_candidate"],
            })
        return _FakeResp(_json.dumps({"jobs": out}))


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


class TestAnalyzerEndToEnd(unittest.TestCase):
    def test_one_batched_call_and_filters(self):
        jobs = [
            Job(title="Databricks Engineer", company="V1", location="Remote", source="gmail",
                description="C2C requirement, $70/hr", employment_hint="C2C", contact_email="rec@vendor.com"),
            Job(title="Java Engineer", company="V2", location="Remote", source="dice",
                description="Snowflake + Java", employment_hint=""),
            Job(title="Databricks Engineer", company="V3", location="Remote", source="dice",
                description="Great role. US Citizens only, no sponsorship.", employment_hint="C2C"),
        ]
        client = _FakeClient()
        an = ClaudeAnalyzer("profile", {"model": "claude-haiku-4-5"}, client=client)
        an.analyze(jobs)
        self.assertEqual(len(client.messages.calls), 1)                     # ONE batched call
        self.assertEqual(client.messages.calls[0]["model"], "claude-haiku-4-5")
        self.assertIn("output_config", client.messages.calls[0])
        self.assertEqual((jobs[0].match_percent, jobs[0].employment_type, jobs[0].contact_email),
                         (92, "C2C", "rec@vendor.com"))
        self.assertEqual(jobs[1].missing_skills, "Snowflake, Java")
        self.assertEqual(jobs[2].visa_status, "Restricted")                  # regex hard guard wins
        kept = [j for j in jobs if j.match_percent >= 80 and j.visa_status != "Restricted"]
        self.assertEqual([j.company for j in kept], ["V1"])


class TestGmailParser(unittest.TestCase):
    def _source(self):
        from agent.config import load_config
        from sources.gmail_imap import GmailSource
        cfg = load_config()
        return GmailSource(cfg, http=None)

    def _raw(self, subject, body, from_="Ravi Kumar | ABC Technologies <ravi@abctech.com>", reply_to=None, html=False):
        from email.message import EmailMessage
        from email.utils import format_datetime
        from datetime import datetime, timezone
        m = EmailMessage()
        m["From"] = from_
        m["To"] = "me@gmail.com"
        m["Subject"] = subject
        m["Date"] = format_datetime(datetime.now(timezone.utc))
        m["Message-ID"] = "<abc@abctech.com>"
        if reply_to:
            m["Reply-To"] = reply_to
        if html:
            m.set_content("plain fallback")
            m.add_alternative(body, subtype="html")
        else:
            m.set_content(body)
        return m.as_bytes()

    def test_vendor_email_is_parsed(self):
        from datetime import datetime, timedelta, timezone
        src = self._source()
        body = (
            "Hi Tarun,\n\nHope you are doing well. Please find the requirement below.\n\n"
            "Job Title: Azure Data Engineer (Databricks)\nClient: Fortune 100 Bank\nLocation: Remote (EST hours)\n"
            "Duration: 12+ months\nRate: $65/hr on C2C\nEmployment Type: C2C / 1099\n\n"
            "Must have: PySpark, Azure Databricks, ADF, Delta Lake, SQL.\n"
            "Apply here: https://jobs.abctech.com/req/12345?utm_source=email\n"
            "Unsubscribe: https://abctech.com/unsubscribe?u=1\n\nThanks,\nRavi\n"
        )
        job = src._parse_message(self._raw("Urgent Requirement: Azure Data Engineer - Remote - C2C", body,
                                           reply_to="hiring@abctech.com"),
                                 cutoff=datetime.now(timezone.utc) - timedelta(hours=3))
        self.assertIsNotNone(job)
        self.assertEqual(job.title, "Azure Data Engineer (Databricks)")
        self.assertEqual(job.company, "Fortune 100 Bank")
        self.assertEqual(job.location, "Remote (EST hours)")
        self.assertEqual(job.rate, "$65/hr on C2C")
        self.assertEqual(job.contact_email, "hiring@abctech.com")          # Reply-To beats From
        self.assertEqual(job.employment_hint, "C2C")
        self.assertEqual(job.url, "https://jobs.abctech.com/req/12345?utm_source=email")
        self.assertEqual(job.job_id, "https://jobs.abctech.com/req/12345")  # tracking param stripped
        self.assertEqual(job.source, "gmail")

    def test_non_job_and_ignored_sender_are_skipped(self):
        from datetime import datetime, timedelta, timezone
        src = self._source()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=3)
        self.assertIsNone(src._parse_message(self._raw("Lunch tomorrow?", "Want to grab lunch at noon?"), cutoff))
        self.assertIsNone(src._parse_message(
            self._raw("New jobs for you: Data Engineer", "Data Engineer contract requirement, client ...",
                      from_="LinkedIn Jobs <jobs-noreply@linkedin.com>"), cutoff))

    def test_html_body_and_subject_title_fallback(self):
        from datetime import datetime, timedelta, timezone
        src = self._source()
        html_body = ("<html><body><p>Hello,</p><p>We have a <b>contract</b> opening with our direct client. "
                     "Skills: Power Apps, Power Automate, Dataverse. Pay rate $70 - $75 per hour, W2 or C2C.</p>"
                     "<p>Regards<br>Priya<br>priya@vendorx.io</p></body></html>")
        job = src._parse_message(self._raw("FW: Power Platform Developer || Remote || 12 Months", html_body, html=True),
                                 cutoff=datetime.now(timezone.utc) - timedelta(hours=3))
        self.assertIsNotNone(job)
        self.assertTrue(job.title.startswith("Power Platform Developer"))
        self.assertIn("$70", job.rate)
        self.assertEqual(job.employment_hint, "C2C")
        self.assertEqual(len(job.job_id), 40)                                # no URL -> sha1 fallback
