#!/usr/bin/env python3
"""Build manga-catalog.json for Kanji Garden from Jiten (https://jiten.moe).

Jiten data is CC BY-SA 4.0. Run this on your computer or let the GitHub Action do it.
  python tools/build_catalog.py --list tools/manga-list.txt --out manga-catalog.json
  python tools/build_catalog.py --search "title words"     (prints Jiten ids to put in the list)
  python tools/build_catalog.py --selftest                 (offline check, no network)
Optional: set JITEN_API_KEY to get Jiten's higher rate limit.
"""
import argparse, collections, datetime, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request

API = "https://api.jiten.moe/api/media-deck"
KANJI = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
DELAY = 0.7  # seconds between requests, about 85 per minute (Jiten allows 120)

def get(path, params=None, tries=6):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"Accept": "application/json", "User-Agent": "kanji-garden-catalog/1.0 (personal project)"}
    if os.environ.get("JITEN_API_KEY"):
        headers["X-Api-Key"] = os.environ["JITEN_API_KEY"]
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                time.sleep(DELAY)
                return json.load(r)
        except urllib.error.HTTPError as e:
            wait = int(e.headers.get("Retry-After", 0) or 0) if e.code == 429 else 0
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(max(wait, 2 ** attempt)); continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(2 ** attempt); continue
            raise

def deck_title(d, default="Untitled"):
    return d.get("englishTitle") or d.get("romajiTitle") or d.get("originalTitle") or default

EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

def download_cover(deck_id, cover_url, folder):
    """Save the cover next to the catalog. Jiten's CDN can resize with ?width=."""
    if not cover_url or cover_url.endswith("nocover.jpg"):
        return None
    url = cover_url
    if "cdn.jiten.moe" in url:
        url += ("&" if "?" in url else "?") + "width=320"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kanji-garden-catalog/1.0 (personal project)"})
        with urllib.request.urlopen(req, timeout=60) as r:
            ext = EXT.get(r.headers.get_content_type(), ".jpg")
            data = r.read(1500000)
        if len(data) < 500:
            return None
        os.makedirs(folder, exist_ok=True)
        name = "jiten-%s%s" % (deck_id, ext)
        open(os.path.join(folder, name), "wb").write(data)
        time.sleep(DELAY)
        return "covers/" + name
    except Exception as e:
        print("cover download failed for id %s: %s" % (deck_id, e), file=sys.stderr)
        return None

def search(q):
    return (get("/get-media-decks", {"titleFilter": q, "offset": 0}) or {}).get("data") or []

def kanji_counts(deck_id):
    counts, offset, total, info = collections.Counter(), 0, None, {}
    while total is None or offset < total:
        resp = get("/%s/vocabulary" % deck_id, {"offset": offset, "limit": 200, "sortBy": "deckFreq", "sortOrder": "Descending"}) or {}
        data = resp.get("data")
        words = (data or {}).get("words") or []
        if not words:
            break
        if not info:
            info = {"deck": data.get("deck") or {}, "parent": data.get("parentDeck") or {}}
        total = resp.get("totalItems") or 0
        for w in words:
            text = (w.get("mainReading") or {}).get("text", "")
            n = int(w.get("occurrences") or 0)
            for ch in KANJI.findall(text):
                counts[ch] += n
        offset += len(words)
    return counts, info

def parse_line(line):
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    name = None
    if "|" in line:
        line, name = [s.strip() for s in line.split("|", 1)]
    m = re.search(r"/decks/media/(\d+)", line) or re.fullmatch(r"(\d+)", line)
    return (int(m.group(1)), None, name) if m else (None, line, name)

def build(list_path, out_path, covers=True):
    old = {}
    if os.path.exists(out_path):
        try:
            old = {t["id"]: t for t in json.load(open(out_path, encoding="utf-8")).get("titles", [])}
        except Exception:
            pass
    titles, failed = [], 0
    for line in open(list_path, encoding="utf-8"):
        p = parse_line(line)
        if not p:
            continue
        deck_id, query, name = p
        try:
            title = name
            if deck_id is None:
                hits = search(query)
                if not hits:
                    raise ValueError("no search results for %r" % query)
                deck_id, title = hits[0]["deckId"], name or deck_title(hits[0])
                print("matched %r -> %s (id %s)" % (query, title, deck_id))
            counts, info = kanji_counts(deck_id)
            if not counts:
                raise ValueError("no vocabulary returned for id %s" % deck_id)
            deck, parent = info.get("deck", {}), info.get("parent", {})
            title = title or deck_title(deck, None) or deck_title(parent, None)
            cover_url = deck.get("coverName") or parent.get("coverName")
            if not title or not cover_url:
                resp = get("/%s/detail" % deck_id) or {}
                detail = resp.get("data") if isinstance(resp.get("data"), dict) else resp
                d_deck = detail.get("mainDeck") or detail.get("deck") or detail
                d_parent = detail.get("parentDeck") or {}
                title = title or deck_title(d_deck, None) or deck_title(d_parent, None)
                cover_url = cover_url or d_deck.get("coverName") or d_parent.get("coverName")
            title = title or "Jiten #%s" % deck_id
            cover = download_cover(deck_id, cover_url, os.path.join(os.path.dirname(out_path) or ".", "covers")) if covers else None
            cover = cover or (old.get(deck_id) or {}).get("cover")
            entry = {"id": deck_id, "title": title,
                     "url": "https://jiten.moe/decks/media/%s/detail" % deck_id,
                     "kc": dict(counts.most_common())}
            if cover:
                entry["cover"] = cover
            titles.append(entry)
            print("ok  %s: %d kanji" % (titles[-1]["title"], len(counts)))
        except Exception as e:
            failed += 1
            print("FAILED %s: %s" % (line.strip(), e), file=sys.stderr)
            if deck_id in old:
                titles.append(old[deck_id]); print("kept previous data for id %s" % deck_id)
    if not titles:
        sys.exit("nothing built")
    json.dump({"generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
               "source": "Jiten (https://jiten.moe), CC BY-SA 4.0", "titles": titles},
              open(out_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("wrote %s with %d titles (%d failed)" % (out_path, len(titles), failed))

def selftest():
    global get, DELAY
    DELAY = 0
    pages = {0: [("猫が好き", 5), ("犬", 2)], 2: [("猫", 1), ("かな", 9)]}
    def fake(path, params=None, tries=6):
        if path == "/get-media-decks":
            return {"data": [{"deckId": 7, "englishTitle": "Test", "originalTitle": "テスト"}]}
        off = params["offset"]
        rows = pages.get(off, [])
        return {"data": {"deck": {"romajiTitle": "Yotsuba to!", "coverName": "https://cdn.jiten.moe/x.jpg"}, "parentDeck": {},
                         "words": [{"mainReading": {"text": t}, "occurrences": n} for t, n in rows]}, "totalItems": 4, "pageSize": 2, "currentOffset": off}
    get = fake
    c, info = kanji_counts(7)
    assert c == {"猫": 6, "好": 5, "犬": 2}, c
    assert deck_title(info["deck"], None) == "Yotsuba to!" and info["deck"]["coverName"].endswith("x.jpg")
    assert deck_title({}, None) is None
    assert search("x")[0]["deckId"] == 7
    assert parse_line("12345 | My Title") == (12345, None, "My Title")
    assert parse_line("https://jiten.moe/decks/media/55166/detail") == (55166, None, None)
    assert parse_line("# comment") is None and parse_line("one punch") == (None, "one punch", None)
    print("selftest ok")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", default="tools/manga-list.txt"); ap.add_argument("--out", default="manga-catalog.json")
    ap.add_argument("--search"); ap.add_argument("--selftest", action="store_true"); ap.add_argument("--no-covers", action="store_true")
    a = ap.parse_args()
    if a.selftest: selftest()
    elif a.search:
        for d in search(a.search):
            print(d.get("deckId"), "|", deck_title(d), "|", d.get("originalTitle"), "| type", d.get("mediaType"))
    else: build(a.list, a.out, covers=not a.no_covers)
