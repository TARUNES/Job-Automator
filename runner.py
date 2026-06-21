"""
Job Tracker Automation
-----------------------
Reads companies.csv (company, careers_url, department_keyword)
Visits each careers page, detects ATS platform, pulls job listings,
filters by department_keyword, and diffs against last run to find NEW roles.

Output:
  - all_jobs_<date>.csv      -> every matching job found this run
  - new_jobs_<date>.csv      -> only jobs not seen in previous runs
  - seen_jobs.json           -> internal state file (don't delete, used for diffing)

Usage:
  python job_tracker.py
  python job_tracker.py --input mycompanies.csv
"""

import csv
import json
import re
import sys
import argparse
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

LOG_CALLBACK = None

def print(*args, **kwargs):
    import builtins
    builtins.print(*args, **kwargs)
    if LOG_CALLBACK:
        msg = " ".join(str(arg) for arg in args)
        LOG_CALLBACK(msg)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
SEEN_FILE = Path("seen_jobs.json")
TIMEOUT = 15


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def detect_ats(url, html):
    host = urlparse(url).netloc.lower()
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "myworkdayjobs.com" in host:
        return "workday"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "ashbyhq.com" in host:
        return "ashby"
    if html and "boards.greenhouse" in html.lower():
        return "greenhouse"
    return "generic"


def extract_greenhouse_token(url):
    """Greenhouse board URLs look like job-boards.greenhouse.io/<token> or
    boards.greenhouse.io/<token>. Pull the token to call the public API."""
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else None


def extract_lever_token(url):
    """Lever URLs look like jobs.lever.co/<token>"""
    path = urlparse(url).path.strip("/")
    return path.split("/")[0] if path else None


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  [ERROR] could not fetch {url}: {e}")
        return None


def parse_lever(html, base_url):
    # kept only as a last-resort fallback if the Lever API call fails for some reason
    soup = BeautifulSoup(html, "lxml")
    jobs = []
    for posting in soup.select("a.posting-title, div.posting"):
        title_el = posting.select_one(".posting-title h5, h5") or posting
        title = title_el.get_text(strip=True)
        href = posting.get("href")
        if title and href:
            jobs.append({"title": title, "url": href})
    return jobs


def parse_generic(html, base_url):
    """
    Fallback: grab anchor tags whose text looks like a job title
    and whose href looks like a job/posting link. Noisy but works
    as a baseline for unknown ATS / custom career pages.
    """
    soup = BeautifulSoup(html, "lxml")
    jobs = []
    job_link_pattern = re.compile(r"(job|career|posting|position|opening)", re.I)
    seen_titles = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        title = a.get_text(strip=True)
        if not title or len(title) < 4 or len(title) > 120:
            continue
        if job_link_pattern.search(href) and title not in seen_titles:
            link = href if href.startswith("http") else "https://" + urlparse(base_url).netloc + href
            jobs.append({"title": title, "url": link})
            seen_titles.add(title)
    return jobs


def jobs_from_greenhouse_api(url):
    """Use Greenhouse's public JSON API instead of scraping HTML.
    Returns all jobs with location. Greenhouse returns all at once (no pagination needed).
    Docs: https://developers.greenhouse.io/job-board.html"""
    token = extract_greenhouse_token(url)
    if not token:
        return None
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        r = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        jobs = []
        for j in data.get("jobs", []):
            location = ""
            if isinstance(j.get("location"), dict):
                location = j["location"].get("name", "")
            elif isinstance(j.get("location"), str):
                location = j["location"]
            jobs.append({
                "title": j["title"],
                "url": j.get("absolute_url", url),
                "location": location.strip()
            })
        return jobs
    except Exception as e:
        print(f"  [WARN] greenhouse API failed: {e}")
        return None


def jobs_from_lever_api(url):
    """Use Lever's public JSON API instead of scraping HTML.
    Lever returns all at once (no pagination needed).
    Docs: https://github.com/lever/postings-api"""
    token = extract_lever_token(url)
    if not token:
        return None
    api_url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    try:
        r = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
        jobs = []
        for j in data:
            categories = j.get("categories", {})
            location = categories.get("location") or categories.get("allLocations", [""])[0] if isinstance(categories.get("allLocations"), list) else ""
            jobs.append({
                "title": j["text"],
                "url": j.get("hostedUrl", url),
                "location": str(location).strip()
            })
        return jobs
    except Exception as e:
        print(f"  [WARN] lever API failed: {e}")
        return None


def jobs_from_workday_api(url):
    """Use Workday's internal JSON API with pagination support.
    Workday URLs look like: https://<company>.wd5.myworkdayjobs.com/<path>
    The API endpoint is: /wday/cxs/<company>/<path>/jobs (POST)
    """
    try:
        parsed = urlparse(url)
        host = parsed.netloc  # e.g. visa.wd5.myworkdayjobs.com
        path_parts = [p for p in parsed.path.strip("/").split("/") if p]
        
        # Extract company subdomain from hostname
        subdomain = host.split(".")[0]  # e.g. 'visa'
        board_path = path_parts[0] if path_parts else subdomain  # e.g. 'Visa'
        
        api_url = f"https://{host}/wday/cxs/{subdomain}/{board_path}/jobs"
        
        all_jobs = []
        offset = 0
        limit = 20
        total_listings = None
        
        while True:
            payload = {
                "appliedFacets": {},
                "limit": limit,
                "offset": offset,
                "searchText": ""
            }
            headers = {
                **HEADERS,
                "Accept": "application/json",
                "Content-Type": "application/json"
            }
            r = requests.post(api_url, json=payload, headers=headers, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"  [WARN] Workday API returned {r.status_code} at offset {offset}")
                break
            
            data = r.json()
            postings = data.get("jobPostings", [])
            if total_listings is None:
                total_listings = data.get("total", 0)
            
            for j in postings:
                title = j.get("title", "")
                ext_url = j.get("externalPath", "")
                job_url = f"https://{host}{ext_url}" if ext_url.startswith("/") else url
                location = j.get("locationsText", "") or j.get("bulletFields", [""])[0] if j.get("bulletFields") else ""
                all_jobs.append({
                    "title": title,
                    "url": job_url,
                    "location": str(location).strip()
                })
            
            offset += len(postings)
            if offset >= total_listings or not postings:
                break
            
            print(f"  [Workday] fetched {offset}/{total_listings} listings...")
            time.sleep(0.5)  # be polite between pages
        
        return all_jobs if all_jobs else None
    except Exception as e:
        print(f"  [WARN] Workday API failed: {e}")
        return None


def get_jobs(url):
    # Try official APIs first — far more reliable, no bot-blocking, structured data.
    ats_guess = detect_ats(url, None)
    if ats_guess == "greenhouse":
        jobs = jobs_from_greenhouse_api(url)
        if jobs is not None:
            print("  source: greenhouse API")
            return dedupe(jobs)
    if ats_guess == "lever":
        jobs = jobs_from_lever_api(url)
        if jobs is not None:
            print("  source: lever API")
            return dedupe(jobs)
    if ats_guess == "workday":
        jobs = jobs_from_workday_api(url)
        if jobs is not None:
            print("  source: Workday API (paginated)")
            return dedupe(jobs)

    # Fall back to HTML scraping for anything else (custom career pages,
    # SmartRecruiters, Ashby, or if the API call failed)
    html = fetch(url)
    if not html:
        return []
    ats = detect_ats(url, html)
    print(f"  source: HTML scrape (detected: {ats})")
    jobs = parse_generic(html, url)
    return dedupe(jobs)


def dedupe(jobs):
    uniq = {}
    for j in jobs:
        # Ensure all jobs have a location field
        if "location" not in j:
            j["location"] = ""
        uniq[j["title"]] = j
    return list(uniq.values())


def matches_department(title, keyword):
    if not keyword:
        return True
    return keyword.lower() in title.lower()


def generate_html(title, rows, total_count, new_count, date_str):
    import html
    table_rows = []
    for r in rows:
        company = r["company"]
        job_title = r["title"]
        url = r["url"]
        location = r.get("location", "")
        keyword = r["department_keyword"]
        is_new = r["is_new"]
        checked_on = r["checked_on"]
        
        is_new_lower = "true" if is_new else "false"
        status_badge = '<span class="badge new">NEW</span>' if is_new else '<span class="badge seen">SEEN</span>'
        
        esc_company = html.escape(company)
        esc_title = html.escape(job_title)
        esc_url = html.escape(url)
        esc_location = html.escape(location)
        esc_keyword = html.escape(keyword)
        
        row_html = f"""                    <tr data-company="{esc_company}" data-title="{esc_title}" data-location="{esc_location}" data-keyword="{esc_keyword}" data-isnew="{is_new_lower}">
                        <td><span class="badge company">{esc_company}</span></td>
                        <td>
                            <a class="job-title-link" href="{esc_url}" target="_blank">
                                {esc_title}
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="7" y1="17" x2="17" y2="7"></line><polyline points="7 7 17 7 17 17"></polyline></svg>
                            </a>
                        </td>
                        <td>
                            {f'<span style="display:inline-flex;align-items:center;gap:0.3rem;color:var(--text-secondary);font-size:0.85rem;"><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path><circle cx="12" cy="10" r="3"></circle></svg>{esc_location}</span>' if esc_location else '<span style="color:#1f2937; font-size:0.8rem;">-</span>'}
                        </td>
                        <td><span style="color: var(--text-secondary); font-size: 0.875rem;">{esc_keyword}</span></td>
                        <td>{status_badge}</td>
                        <td><span style="color: var(--text-secondary); font-size: 0.875rem;">{checked_on}</span></td>
                    </tr>"""
        table_rows.append(row_html)
        
    table_rows_str = "\n".join(table_rows)
    
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{TITLE}}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-color: #0b0f19;
            --card-bg: #111827;
            --border-color: #1f2937;
            --text-primary: #f3f4f6;
            --text-secondary: #9ca3af;
            --accent: #6366f1;
            --accent-hover: #4f46e5;
            --success: #10b981;
            --success-bg: rgba(16, 185, 129, 0.1);
            --info: #3b82f6;
            --info-bg: rgba(59, 130, 246, 0.1);
            --font-main: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }
        
        body {
            background-color: var(--bg-color);
            color: var(--text-primary);
            font-family: var(--font-main);
            line-height: 1.5;
            padding: 2rem 1rem;
        }
        
        .container {
            max-width: 1100px;
            margin: 0 auto;
        }
        
        header {
            margin-bottom: 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 1rem;
        }
        
        .title-area h1 {
            font-size: 2rem;
            font-weight: 700;
            background: linear-gradient(to right, #ffffff, #9ca3af);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin-bottom: 0.25rem;
        }
        
        .title-area p {
            color: var(--text-secondary);
            font-size: 0.95rem;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1.25rem;
            margin-bottom: 2rem;
        }
        
        .stat-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 1.5rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            position: relative;
            overflow: hidden;
        }
        
        .stat-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            width: 4px;
            height: 100%;
            background: var(--accent);
        }
        
        .stat-card.new::before {
            background: var(--success);
        }
        
        .stat-label {
            color: var(--text-secondary);
            font-size: 0.875rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        
        .stat-value {
            font-size: 2.25rem;
            font-weight: 700;
            color: var(--text-primary);
        }
        
        .controls {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
            gap: 1rem;
            flex-wrap: wrap;
        }
        
        .search-box {
            position: relative;
            flex-grow: 1;
            max-width: 400px;
        }
        
        .search-box input {
            width: 100%;
            padding: 0.75rem 1rem 0.75rem 2.5rem;
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-primary);
            font-family: var(--font-main);
            font-size: 0.95rem;
            transition: all 0.2s ease;
        }
        
        .search-box input:focus {
            outline: none;
            border-color: var(--accent);
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.15);
        }
        
        .search-box svg {
            position: absolute;
            left: 0.85rem;
            top: 50%;
            transform: translateY(-50%);
            color: var(--text-secondary);
            pointer-events: none;
        }
        
        .filters {
            display: flex;
            gap: 0.5rem;
        }
        
        .filter-btn {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            color: var(--text-secondary);
            padding: 0.6rem 1.2rem;
            font-size: 0.9rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        
        .filter-btn:hover {
            color: var(--text-primary);
            border-color: var(--text-secondary);
        }
        
        .filter-btn.active {
            background: var(--accent);
            border-color: var(--accent);
            color: white;
        }
        
        .table-container {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
            text-align: left;
        }
        
        th {
            background-color: rgba(255, 255, 255, 0.02);
            color: var(--text-secondary);
            font-weight: 600;
            font-size: 0.85rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            padding: 1rem 1.5rem;
            border-bottom: 1px solid var(--border-color);
        }
        
        td {
            padding: 1.2rem 1.5rem;
            border-bottom: 1px solid var(--border-color);
            font-size: 0.95rem;
            vertical-align: middle;
        }
        
        tr:last-child td {
            border-bottom: none;
        }
        
        tr:hover td {
            background-color: rgba(255, 255, 255, 0.01);
        }
        
        .job-title-link {
            color: var(--text-primary);
            text-decoration: none;
            font-weight: 600;
            transition: color 0.15s ease;
            display: inline-flex;
            align-items: center;
            gap: 0.25rem;
        }
        
        .job-title-link:hover {
            color: var(--accent);
        }
        
        .job-title-link svg {
            opacity: 0;
            transition: opacity 0.15s ease, transform 0.15s ease;
            transform: translate(-2px, 2px);
        }
        
        .job-title-link:hover svg {
            opacity: 1;
            transform: translate(0, 0);
        }
        
        .badge {
            display: inline-flex;
            align-items: center;
            padding: 0.25rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            font-weight: 600;
            letter-spacing: 0.02em;
        }
        
        .badge.new {
            background-color: var(--success-bg);
            color: var(--success);
            border: 1px solid rgba(16, 185, 129, 0.2);
            animation: pulse-border 2s infinite;
        }
        
        .badge.seen {
            background-color: rgba(156, 163, 175, 0.1);
            color: var(--text-secondary);
            border: 1px solid rgba(156, 163, 175, 0.2);
        }
        
        .badge.company {
            background-color: rgba(99, 102, 241, 0.1);
            color: var(--accent);
            border: 1px solid rgba(99, 102, 241, 0.2);
        }
        
        .no-jobs {
            padding: 3rem 1.5rem;
            text-align: center;
            color: var(--text-secondary);
            font-size: 1.1rem;
        }
        
        @keyframes pulse-border {
            0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.4); }
            70% { box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }
        
        @media (max-width: 640px) {
            .controls {
                flex-direction: column;
                align-items: stretch;
            }
            .search-box {
                max-width: 100%;
            }
            th, td {
                padding: 0.75rem 1rem;
            }
            th:nth-child(4), td:nth-child(4) {
                display: none;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="title-area">
                <h1>{{TITLE}}</h1>
                <p>Generated on {{DATE_STR}} • Auto-detected job openings</p>
            </div>
        </header>
        
        <div class="stats-grid">
            <div class="stat-card">
                <span class="stat-label">Total Jobs Found</span>
                <span class="stat-value">{{TOTAL_COUNT}}</span>
            </div>
            <div class="stat-card new">
                <span class="stat-label">New Roles Since Last Run</span>
                <span class="stat-value">{{NEW_COUNT}}</span>
            </div>
        </div>
        
        <div class="controls">
            <div class="search-box">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
                <input type="text" id="searchInput" placeholder="Search by title, company, location, or keyword..." oninput="filterJobs()">
            </div>
            <div class="filters">
                <button class="filter-btn active" id="btnAll" onclick="setFilter('all')">Show All</button>
                <button class="filter-btn" id="btnNew" onclick="setFilter('new')">New Only ({{NEW_COUNT}})</button>
            </div>
        </div>
        
        <div class="table-container">
            <table id="jobsTable">
                <thead>
                    <tr>
                        <th style="width: 15%">Company</th>
                        <th style="width: 40%">Job Title</th>
                        <th style="width: 15%">Location</th>
                        <th style="width: 10%">Keyword</th>
                        <th style="width: 10%">Status</th>
                        <th style="width: 10%">Date</th>
                    </tr>
                </thead>
                <tbody>
{{TABLE_ROWS}}
                </tbody>
            </table>
            <div id="noJobsMessage" class="no-jobs" style="display: none;">
                No matching jobs found.
            </div>
        </div>
    </div>
    
    <script>
        let currentFilter = 'all';
        
        function filterJobs() {
            const searchVal = document.getElementById('searchInput').value.toLowerCase();
            const table = document.getElementById('jobsTable');
            const trs = table.getElementsByTagName('tbody')[0].getElementsByTagName('tr');
            let visibleCount = 0;
            
            for (let i = 0; i < trs.length; i++) {
                const tr = trs[i];
                const company = tr.getAttribute('data-company').toLowerCase();
                const title = tr.getAttribute('data-title').toLowerCase();
                const location = tr.getAttribute('data-location') ? tr.getAttribute('data-location').toLowerCase() : '';
                const keyword = tr.getAttribute('data-keyword').toLowerCase();
                const isNew = tr.getAttribute('data-isnew') === 'true';
                
                const matchesSearch = company.includes(searchVal) || title.includes(searchVal) || location.includes(searchVal) || keyword.includes(searchVal);
                const matchesFilter = currentFilter === 'all' || (currentFilter === 'new' && isNew);
                
                if (matchesSearch && matchesFilter) {
                    tr.style.display = '';
                    visibleCount++;
                } else {
                    tr.style.display = 'none';
                }
            }
            
            document.getElementById('noJobsMessage').style.display = visibleCount === 0 ? 'block' : 'none';
        }
        
        function setFilter(filterType) {
            currentFilter = filterType;
            document.getElementById('btnAll').classList.toggle('active', filterType === 'all');
            document.getElementById('btnNew').classList.toggle('active', filterType === 'new');
            filterJobs();
        }
    </script>
</body>
</html>
"""
    return html_template.replace("{{TITLE}}", title)\
                         .replace("{{DATE_STR}}", date_str)\
                         .replace("{{TOTAL_COUNT}}", str(total_count))\
                         .replace("{{NEW_COUNT}}", str(new_count))\
                         .replace("{{TABLE_ROWS}}", table_rows_str)


def run_job_tracker(input_file="companies.csv", callback=None, companies_list=None):
    global LOG_CALLBACK
    LOG_CALLBACK = callback

    seen = load_seen()
    today = date.today().isoformat()

    all_rows = []
    new_rows = []

    if companies_list is not None:
        rows = companies_list
    else:
        if not Path(input_file).exists():
            print(f"Input file not found: {input_file}")
            return False

        with open(input_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

    for row in rows:
        company = row["company"].strip()
        url = row["careers_url"].strip()
        keyword = row.get("department_keyword", "").strip()

        print(f"\nChecking {company} -> {url} (filter: '{keyword}')")
        jobs = get_jobs(url)
        filtered = [j for j in jobs if matches_department(j["title"], keyword)]
        print(f"  found {len(jobs)} total listings, {len(filtered)} matching '{keyword}'")

        company_seen = set(seen.get(company, []))
        for j in filtered:
            is_new = j["title"] not in company_seen
            record = {
                "company": company,
                "title": j["title"],
                "url": j["url"],
                "location": j.get("location", ""),
                "department_keyword": keyword,
                "checked_on": today,
                "is_new": is_new,
            }
            all_rows.append(record)
            if is_new:
                new_rows.append(record)

        seen[company] = list({j["title"] for j in filtered} | company_seen)
        time.sleep(1)  # be polite

    save_seen(seen)

    # Save to consolidated jobs_db.json
    db_file = Path("jobs_db.json")
    db_jobs = {}
    if db_file.exists():
        try:
            for job in json.loads(db_file.read_text(encoding="utf-8")):
                db_jobs[(job["company"], job["title"])] = job
        except Exception:
            pass

    # Update with current run's jobs
    for r in all_rows:
        db_jobs[(r["company"], r["title"])] = r

    try:
        db_file.write_text(json.dumps(list(db_jobs.values()), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  [ERROR] Failed to save jobs_db.json: {e}")

    all_path = f"all_jobs_{today}.html"
    new_path = f"new_jobs_{today}.html"

    all_html = generate_html("All Matching Jobs", all_rows, len(all_rows), len(new_rows), today)
    new_html = generate_html("New Matching Jobs Only", new_rows, len(all_rows), len(new_rows), today)

    Path(all_path).write_text(all_html, encoding="utf-8")
    Path(new_path).write_text(new_html, encoding="utf-8")

    print(f"\nDone. {len(all_rows)} matching roles total, {len(new_rows)} new since last run.")
    print(f"  -> {all_path}")
    print(f"  -> {new_path}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="companies.csv", help="CSV with company,careers_url,department_keyword")
    args = parser.parse_args()

    success = run_job_tracker(args.input)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()