from zypher_agent.db import InMemoryRepo
from zypher_agent.leads import clean_email, import_rows, normalize_domain, parse_csv

HEADER = "LeadID,CompanyName,Website,ContactName,Email,Country,Industry,Notes\n"


def write(tmp_path, body):
    p = tmp_path / "leads.csv"
    p.write_text(HEADER + body, encoding="utf-8")
    return p


def test_normalize_domain():
    assert normalize_domain("https://www.Example.com/about") == "example.com"
    assert normalize_domain("example.co.uk") == "example.co.uk"
    assert normalize_domain("not a site") is None
    assert normalize_domain("") is None


def test_clean_email():
    assert clean_email(" Info@Example.COM ") == "info@example.com"
    assert clean_email("nope") is None


def test_parse_skips_bad_rows(tmp_path):
    p = write(tmp_path, (
        "1,Good,good.com,Ann,ann@good.com,India,Retail,\n"
        "2,NoSite,,Bob,bob@x.com,,,\n"
        "3,NoMail,nomail.com,,,,,\n"
        "4,Dup,dup.com,,ann@good.com,,,\n"
    ))
    parsed = parse_csv(p)
    assert len(parsed.rows) == 1 and parsed.rows[0][1].legacy_lead_id == "1"
    assert [line for line, _ in parsed.skipped] == [3, 4, 5]


def test_import_honours_suppression_and_is_repeatable(tmp_path):
    parsed = parse_csv(write(tmp_path, (
        "1,A,a.com,Ann,ann@a.com,,,\n"
        "2,B,b.com,Bo,bo@b.com,,,\n"
        "3,C,c.com,Cy,cy@c.com,,,\n"
    )))
    repo = InMemoryRepo()
    repo.add_suppression("unsubscribe", email="ANN@a.com")
    repo.add_suppression("manual", domain="c.com")
    assert import_rows(repo, parsed.rows) == (1, 2)
    assert import_rows(repo, parsed.rows) == (1, 2)
    assert len(repo.leads_with_status("new", 100)) == 1


def test_csv_import_skips_companies_we_rejected_before(tmp_path):
    parsed = parse_csv(write(tmp_path, (
        "1,A,a.com,Ann,ann@a.com,,,\n"
        "2,B,b.com,Bo,bo@b.com,,,\n"
    )))
    repo = InMemoryRepo()
    first = repo.upsert_lead(*parsed.rows[0])
    repo.reject_company(repo.get_lead(first).company_id, "too_small_or_informal", icp_version=7)
    assert import_rows(repo, parsed.rows) == (1, 1)  # (imported, skipped)
    assert not repo.company_exists("a.com")
