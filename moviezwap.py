#!/usr/bin/env python3

import re, time, json, threading, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote_plus

BASE    = "https://www.moviezwap.codes"
CDN_PAT = re.compile(r'https://\d+g\d+\.moviezzwaphd\.xyz[^\s"\'<>\)]+', re.I)
WORKERS = 20
LOCK    = threading.Lock()

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
})
adapter = requests.adapters.HTTPAdapter(pool_connections=40, pool_maxsize=40)
S.mount("https://", adapter)
S.mount("http://", adapter)

try:
    S.get(BASE + "/", timeout=15)
    S.headers.update({"Referer": BASE + "/", "Sec-Fetch-Site": "same-origin"})
except Exception:
    pass


def get_html(url: str, timeout: int = 15, referer: str = None) -> requests.Response:
    hdrs = {}
    if referer:
        hdrs["Referer"] = referer
    r = S.get(url, timeout=timeout, headers=hdrs)
    r.raise_for_status()
    return r


def page_url(base: str, page: int) -> str:
    if page == 1:
        return base
    stem = base.rstrip("/")
    if stem.endswith(".html"):
        stem = stem[:-5]
    return f"{stem}/{page}.html"


def movie_links_from_html(html: str) -> set:
    return {a["href"] for a in BeautifulSoup(html, "lxml").find_all("a", href=True)
            if a["href"].startswith("/movie/")}


def extract_quality(text: str, filename: str = "") -> str:
    for src in (text, filename):
        m = re.search(r'(2160p|1080p|720p|480p|360p|320p|240p)', src, re.I)
        if m:
            return m.group(1).upper()
    m = re.search(r'\b(HD|SD|HQ|4K|FHD)\b', text, re.I)
    return m.group(1).upper() if m else text.strip() or "?"


def _size_from_text(text: str) -> str:
    m = re.search(r'(\d+(?:\.\d+)?)\s*(MB|GB)', text, re.I)
    return f"{m.group(1)} {m.group(2).upper()}" if m else "?"


def resolve_cdn(dwload_url: str, label: str = "") -> dict | None:
    try:
        file_id = re.search(r'file=([^&]+)', dwload_url)
        file_id = file_id.group(1) if file_id else dwload_url.split("file=")[-1]

        dw_url = f"{BASE}/dwload.php?file={file_id}"
        dl_url = f"{BASE}/download.php?file={file_id}"

        S.get(dw_url, timeout=14, headers={"Referer": BASE + "/"})
        r2 = S.get(dl_url, timeout=14, headers={"Referer": dw_url})
        text = r2.text
        soup2 = BeautifulSoup(text, "lxml")

        cdn = ""
        for a in soup2.find_all("a", href=True):
            if "moviezzwaphd.xyz" in a["href"]:
                cdn = a["href"]
                break

        if not cdn:
            m = re.search(
                r'(?:window\.location|location\.href)\s*=\s*["\']([^"\']+moviezzwaphd[^"\']+)["\']',
                text)
            if m:
                cdn = m.group(1)

        if not cdn:
            m = CDN_PAT.search(text)
            if m:
                cdn = m.group(0).rstrip("\\")

        if not cdn:
            return None

        filename = cdn.split("?")[0].rstrip("/").split("/")[-1]
        size = _size_from_text(text)
        return {"quality": extract_quality(label, filename),
                "filename": filename, "size": size, "cdn_url": cdn}

    except Exception:
        return None


def get_movie_cdns(movie_url: str, verbose: bool = False) -> list[dict]:
    full_url = movie_url if movie_url.startswith("http") else urljoin(BASE, movie_url)
    r = get_html(full_url, referer=BASE + "/")
    s = BeautifulSoup(r.text, "lxml")

    title_tag = s.find("h1") or s.find("h2")
    title = title_tag.get_text(strip=True) if title_tag else full_url

    dwload_links = []
    for a in s.find_all("a", href=True):
        href = a["href"]
        if "dwload.php" in href or "download.php" in href:
            full_href = urljoin(BASE, href) if not href.startswith("http") else href
            label = a.get_text(strip=True) or "Download"
            dwload_links.append((label, full_href))

    if not dwload_links:
        if verbose:
            print(f"  ⚠️  No download links found on: {full_url}")
        return []

    if verbose:
        print(f"\n  🎬 {title}")
        print(f"  Found {len(dwload_links)} download option(s) — resolving CDN links…\n")

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {pool.submit(resolve_cdn, href, label): label
                      for label, href in dwload_links}
        for f in as_completed(future_map):
            cdn = f.result()
            if cdn:
                results.append(cdn)

    def quality_sort_key(x):
        m = re.search(r'(\d+)p', x.get("quality", ""), re.I)
        return int(m.group(1)) if m else 0

    results.sort(key=quality_sort_key)
    return results


def print_cdn_results(results: list[dict]):
    if not results:
        print("  ❌ Could not resolve any CDN links.")
        return
    print(f"  {'QUALITY':<10} {'SIZE':<10} FILENAME")
    print("  " + "─" * 75)
    for r in results:
        print(f"  {r['quality']:<10} {r['size']:<10} {r['filename']}")
    print()
    for r in results:
        print(f"  ✅ [{r['quality']}] ({r['size']})")
        print(f"     {r['cdn_url']}")
        print()


def search_movies(query: str) -> list[dict]:
    search_url = f"{BASE}/search.php?q={quote_plus(query)}"
    r = get_html(search_url, referer=BASE + "/")
    s = BeautifulSoup(r.text, "lxml")

    seen = set()
    results = []
    for a in s.find_all("a", href=True):
        href = a["href"]
        if "/movie/" not in href or href in seen:
            continue
        seen.add(href)
        title_text = re.sub(r'^[»\s]+', '', a.get_text(strip=True)).strip()
        if title_text and len(title_text) > 3:
            results.append({"title": title_text, "url": urljoin(BASE, href)})

    url_map = {}
    for item in results:
        url = item["url"]
        if url not in url_map or len(item["title"]) > len(url_map[url]["title"]):
            url_map[url] = item

    final = list(url_map.values())
    words = query.lower().split()
    filtered = [r for r in final
                if any(w in r["title"].lower() or w in r["url"].lower() for w in words)]

    return (filtered if filtered else final)[:20]


def option1_search():
    print("\n" + "─" * 60)
    query = input("  Enter movie name to search: ").strip()
    if not query:
        return

    print(f"\n  Searching for '{query}'…")
    results = search_movies(query)

    if not results:
        print("  ❌ No results found. Try a different name.")
        return

    lang_order = []
    lang_map = {}
    idx = 1
    for r in results:
        title = r["title"]
        lang = "Other"
        for candidate in ["Telugu", "Tamil", "Hindi", "Malayalam", "Kannada",
                          "Bengali", "Marathi", "Hollywood", "Japanese",
                          "Filipino", "Korean", "Chinese"]:
            if candidate.lower() in title.lower():
                lang = candidate
                break
        if lang not in lang_map:
            lang_map[lang] = []
            lang_order.append(lang)
        lang_map[lang].append((idx, r))
        idx += 1

    print(f"\n  Found {len(results)} result(s):\n")
    for lang in lang_order:
        print(f"  ── {lang} " + "─" * (40 - len(lang)))
        for i, r in lang_map[lang]:
            clean = re.sub(r'\s*[-–]\s*\[.*?\]', '', r['title']).strip()
            print(f"  [{i:>2}]  {clean}")
        print()

    while True:
        pick = input("  Enter number to get download links (or 0 to go back): ").strip()
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(results):
            chosen = results[int(pick) - 1]
            break
        print("  Invalid. Try again.")

    cdns = get_movie_cdns(chosen["url"], verbose=True)
    print_cdn_results(cdns)

    if cdns:
        save = input("  Save links to file? (y/n): ").strip().lower()
        if save == "y":
            fname = re.sub(r'[^\w\s-]', '', chosen['title'])[:50].strip() + "_links.txt"
            with open(fname, "w") as f:
                f.write(f"Title: {chosen['title']}\n")
                f.write(f"Page:  {chosen['url']}\n\n")
                for r in cdns:
                    f.write(f"[{r['quality']}] {r['filename']} ({r['size']})\n")
                    f.write(f"{r['cdn_url']}\n\n")
            print(f"  ✅ Saved → {fname}")


def get_categories() -> dict:
    r = get_html(BASE + "/")
    s = BeautifulSoup(r.text, "lxml")
    cats = {}
    for a in s.find_all("a", href=True):
        href = a["href"]
        if "/category/" in href:
            full  = urljoin(BASE, href)
            title = a.get_text(strip=True)
            if full not in cats and title:
                cats[full] = title
    return cats


def scan_category(url: str, title: str) -> tuple:
    seen = set()
    page = 1
    while True:
        try:
            r     = get_html(page_url(url, page))
            links = movie_links_from_html(r.text)
            new   = links - seen
            if not new:
                break
            seen |= new
            page += 1
        except Exception:
            break
    with LOCK:
        print(f"  ✅ {title[:52]:<52}  {page-1:>4}pg  {len(seen):>6} titles")
    return title, list(seen)


def option2_full_scan():
    print("\n" + "─" * 60)
    print("  Full site scan — this takes 1-2 min, grab a coffee ☕")
    print("─" * 60)

    cats = get_categories()
    print(f"\n  Found {len(cats)} categories — scanning with {WORKERS} workers…\n")

    t0 = time.time()
    all_movies = {}
    cat_results = []

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(scan_category, url, title): title
                   for url, title in cats.items()}
        for f in as_completed(futures):
            cat_title, movie_urls = f.result()
            cat_results.append((cat_title, len(movie_urls)))
            for mu in movie_urls:
                if mu not in all_movies:
                    all_movies[mu] = cat_title

    elapsed = time.time() - t0
    cat_results.sort(key=lambda x: -x[1])

    print()
    print("=" * 68)
    print("  BREAKDOWN")
    print("=" * 68)
    for cat, count in cat_results:
        bar = "█" * min(count // 50, 40)
        print(f"  {cat:<52}  {count:>6}  {bar}")
    print()
    print(f"  TOTAL UNIQUE TITLES : {len(all_movies):,}")
    print(f"  TIME TAKEN          : {elapsed:.1f}s")
    print("=" * 68)

    out = {
        "total_titles": len(all_movies),
        "elapsed_seconds": round(elapsed, 1),
        "categories": [{"name": c, "count": n} for c, n in cat_results],
        "movies": [{"url": urljoin(BASE, u), "category": c}
                   for u, c in all_movies.items()]
    }
    with open("moviezwap_full_scan.json", "w") as f:
        json.dump(out, f, indent=2)
    print(f"\n  ✅ Saved {len(all_movies)} titles → moviezwap_full_scan.json")

    print()
    ans = input("  Want to get CDN links for a specific movie from scan? (y/n): ").strip().lower()
    if ans == "y":
        q = input("  Type part of movie name: ").strip().lower()
        matches = [(u, c) for u, c in all_movies.items() if q in u.lower()][:15]
        if not matches:
            print("  Not found in scan.")
            return
        for i, (u, c) in enumerate(matches, 1):
            print(f"  [{i}] {u}  [{c}]")
        pick = input("  Pick number: ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(matches):
            chosen_url = urljoin(BASE, matches[int(pick)-1][0])
            cdns = get_movie_cdns(chosen_url)
            print_cdn_results(cdns)


def option3_paste_url():
    print("\n" + "─" * 60)
    url = input("  Paste movie page URL: ").strip()
    if not url:
        return

    if not url.startswith("http"):
        url = urljoin(BASE, url)

    print(f"\n  Fetching CDN links from: {url}")
    cdns = get_movie_cdns(url, verbose=True)
    print_cdn_results(cdns)

    if cdns:
        save = input("  Save links to file? (y/n): ").strip().lower()
        if save == "y":
            slug = url.rstrip("/").split("/")[-1].replace(".html", "")
            fname = f"{slug}_links.txt"
            with open(fname, "w") as f:
                f.write(f"Page: {url}\n\n")
                for r in cdns:
                    f.write(f"[{r['quality']}] {r['filename']} ({r['size']})\n")
                    f.write(f"{r['cdn_url']}\n\n")
            print(f"  ✅ Saved → {fname}")


def option4_latest():
    print("\n" + "─" * 60)
    print("  Fetching latest 10 movies from homepage…\n")

    r = get_html(BASE + "/")
    s = BeautifulSoup(r.text, "lxml")

    seen = set()
    latest = []
    for a in s.find_all("a", href=True):
        href = a["href"]
        if "/movie/" not in href or href in seen:
            continue
        seen.add(href)
        title_text = re.sub(r'^[»\s]+', '', a.get_text(strip=True)).strip()
        title_text = re.sub(r'\s*[-–]\s*\[.*?\]', '', title_text).strip()
        if title_text and len(title_text) > 3:
            latest.append({"title": title_text, "url": urljoin(BASE, href)})
        if len(latest) == 10:
            break

    if not latest:
        print("  ❌ Could not fetch latest movies.")
        return

    print(f"  Resolving CDN links for all 10…\n")

    results = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        future_map = {pool.submit(get_movie_cdns, m["url"]): m for m in latest}
        with tqdm(total=len(latest), unit="movie", ncols=60) as pbar:
            for f in as_completed(future_map):
                m = future_map[f]
                cdns = f.result()
                results[m["url"]] = cdns
                pbar.update(1)

    print()
    print("=" * 68)
    for m in latest:
        cdns = results.get(m["url"], [])
        print(f"\n  🎬 {m['title']}")
        if cdns:
            for c in cdns:
                print(f"     [{c['quality']}] ({c['size']})  {c['cdn_url']}")
        else:
            print("     ❌ No CDN links resolved")
    print()

    save = input("  Save all links to file? (y/n): ").strip().lower()
    if save == "y":
        fname = "latest_movies_links.txt"
        with open(fname, "w") as f:
            for m in latest:
                cdns = results.get(m["url"], [])
                f.write(f"\n{'='*60}\n{m['title']}\n{m['url']}\n")
                for c in cdns:
                    f.write(f"  [{c['quality']}] {c['filename']} ({c['size']})\n")
                    f.write(f"  {c['cdn_url']}\n")
        print(f"  ✅ Saved → {fname}")


def main():
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║           MoviezWap All-in-One Tool                         ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print("║  [1]  Search movie name → pick → get CDN download links     ║")
    print("║  [2]  Full site scan  → list all titles + details           ║")
    print("║  [3]  Paste movie URL → get CDN links instantly             ║")
    print("║  [4]  Latest 10 movies → fetch all CDN links                ║")
    print("║  [0]  Exit                                                   ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    while True:
        choice = input("  Choose option (0-4): ").strip()
        if choice == "1":
            option1_search()
        elif choice == "2":
            option2_full_scan()
        elif choice == "3":
            option3_paste_url()
        elif choice == "4":
            option4_latest()
        elif choice == "0":
            print("\n  later bro 👋\n")
            sys.exit(0)
        else:
            print("  Invalid. Enter 0, 1, 2, 3 or 4.")

        print()
        again = input("  Back to menu? (y/n): ").strip().lower()
        if again != "y":
            print("\n  later bro 👋\n")
            break
        print()
        print("╔══════════════════════════════════════════════════════════════╗")
        print("║  [1] Search  [2] Full scan  [3] Paste URL  [4] Latest  [0] Exit ║")
        print("╚══════════════════════════════════════════════════════════════╝")
        print()


if __name__ == "__main__":
    main()
