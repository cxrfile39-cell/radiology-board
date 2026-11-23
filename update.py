#!/usr/bin/env python3
"""Weekly update of data.json from PubMed (no AI).

For each panel: search PubMed for guideline / consensus / appropriateness
criteria / RADS-type articles added in the last DAYS days that mention
imaging, classify them by Publication Type, attach the journal IF from
the IF table, prepend them as this week's batch, and drop items older
than KEEP_DAYS (old weeks stay viewable through the page's week menu).
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

DATA = os.environ.get("DATA_FILE", "data.json")
DAYS = int(os.environ.get("DAYS", "14"))           # search window
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "84"))  # 12 weeks of history
PER_PANEL = int(os.environ.get("PER_PANEL", "10"))  # max new items per panel
API_KEY = os.environ.get("NCBI_API_KEY", "")
EU = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

# Clarivate JIF, from journal / publisher / society pages. Edit when JCR updates.
# key = PubMed journal abbreviation (esummary "source"); value = (IF, year)
IF = {
    "Radiology": ("17.6", "2025"),
    "Radiol Artif Intell": ("20.1", "2025"),
    "Radiol Imaging Cancer": ("7.2", "2025"),
    "Radiographics": ("6.2", "2025"),
    "Radiol Cardiothorac Imaging": ("4.5", "2025"),
    "Eur Radiol": ("6.0", "2025"),
    "Insights Imaging": ("5.6", "2025"),
    "Eur Radiol Exp": ("4.7", "2025"),
    "J Am Coll Radiol": ("5.3", "2025"),
    "Cardiovasc Intervent Radiol": ("3.4", "2025"),
    "Abdom Radiol (NY)": ("2.3", "2025"),
    "Nat Rev Endocrinol": ("39.1", "2025"),
}

IMG = ('(imaging[tiab] OR radiolog*[tiab] OR CT[tiab] OR "computed tomography"[tiab] OR MRI[tiab] '
       'OR "magnetic resonance"[tiab] OR ultrasound[tiab] OR ultrasonograph*[tiab] OR PET[tiab] '
       'OR SPECT[tiab] OR angiograph*[tiab] OR mammograph*[tiab] OR "image-guided"[tiab])')
KIND = ('(guideline[pt] OR practice guideline[pt] OR consensus statement[pt] OR consensus[ti] '
        'OR "appropriateness criteria"[ti] OR recommendations[ti] OR guideline*[ti] OR "practice parameter"[ti] '
        'OR "position statement"[ti] OR "position paper"[ti] OR "standards of practice"[ti] OR "practice guidance"[ti] '
        'OR "white paper"[ti] OR (("reporting and data system"[ti] OR RADS[ti]) AND (version[ti] OR update*[ti] OR lexicon[ti])))')
# Title must look like a guideline / consensus document (unless PubMed already tags it Guideline / Consensus Statement)
STRONG = re.compile(r"guideline|consensus|recommendations|appropriateness criteria|practice parameter|position (statement|paper)|"
                    r"standards? of practice|practice guidance|white paper|delphi|statement", re.I)
# Studies that merely compare against "consensus" / test tools against guidelines
NOISE = re.compile(r"consensus (clustering|graph|morpholog)|physician-consensus|agreement between|diagnostic agreement|"
                   r"accuracy|performance of|large language model|chatgpt|gpt-|readability|adherence|concordan|"
                   r"guideline-(directed|based|concordant)|survey|trial endpoints|preclinical|canine|feline|veterinar|"
                   r"recommendation (system|performance)|framework for|parcellation|nutrition|\bendoscop", re.I)

# Extra title words per panel (besides the panel's own keywords) that count as "this panel's topic".
EXTRA = {
    "neuro": r"head injur|intracranial|skull|spinal|glioma|epilep|dementia|alzheimer|neuroimaging|cns\b",
    "chest": r"thorac|mediastin|asthma|nsclc|bronch|lung cancer|interstitial",
    "abd": r"colon|colorectal|rect(al|um)|esophag|oesophag|stomach|spleen|cholangio|bile|gallbladder|steato|cirrho",
    "gu": r"testic|penile|urotheli|urinary|nephr|scrot",
    "breast": r"mammar",
    "women": r"gynec|gynaec|cervical cancer|fetal|fetus",
    "er": r"injur|acute|polytrauma",
    "vasc": r"arter|vein|venous|aneurysm|embol|thromb",
    "vir": r"embolization|percutaneous|catheter|stent|venous|arterial|ischemi",
    "io": r"liver tumou?r|hepatocellular|hcc\b|metasta|brachytherapy|radioembol|chemoembol",
    "nir": r"aneurysm|stroke|intracranial|cerebr|carotid",
    "nm": r"nuclear|scintig|radionuclide|radiopharm|psma|fdg",
    "mol": r"pet\b|psma|fdg|radiotracer|nuclear",
    "cvi": r"cardiac|heart|valv",
    "info": r"report(ing)?\b|workflow|structured",
    "ai": r"machine learning|algorithm",
    "safety": r"contrast|radiation|safety|radiation dose|gadolinium|iodinated",
}


def topic_re(s):
    """Regex built from the panel's (first) keyword group + EXTRA, matched against the title."""
    q = s["q"]
    m = re.match(r"\(\((.*?)\)", q)          # compound query: use the topic group only
    group = m.group(1) if m else q
    terms = [t.strip() for t in re.split(r"\s+OR\s+|\s+AND\s+|[()]", group) if t.strip()]
    pats = [r"\b" + re.escape(t).replace(r"\*", r"\w*").replace(r"\ ", r"\s+") for t in terms]
    if s["id"] in EXTRA:
        pats.append(EXTRA[s["id"]])
    return re.compile("|".join(pats), re.I)


def tiab(q):
    """Restrict a panel's keyword query to title/abstract so affiliations or MeSH explosion don't match."""
    out = []
    for p in re.split(r"(\(|\)|\s+OR\s+|\s+AND\s+)", q):
        t = p.strip()
        if not t or t in "()" or t in ("OR", "AND"):
            out.append(p)
        else:
            out.append(f'"{t}"[tiab]' if " " in t else f"{t}[tiab]")
    return "".join(out)
EXCLUDE = '(animals[mh] NOT humans[mh]) OR "published erratum"[pt] OR retracted publication[pt]'

TZ = timezone(timedelta(hours=8))  # Asia/Taipei


def get(url):
    if API_KEY:
        url += "&api_key=" + API_KEY
    for attempt in range(4):
        try:
            time.sleep(0.12 if API_KEY else 0.4)
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            print(f"  retry {attempt + 1}: {e}", file=sys.stderr)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("PubMed request failed: " + url)


def esearch(term):
    u = (EU + "esearch.fcgi?db=pubmed&retmode=json&sort=pub_date&retmax=%d&datetype=edat&reldate=%d&term=%s"
         % (PER_PANEL * 3, DAYS, urllib.parse.quote(term)))
    return get(u).get("esearchresult", {}).get("idlist", [])


def esummary(ids):
    out = {}
    for i in range(0, len(ids), 150):
        r = get(EU + "esummary.fcgi?db=pubmed&retmode=json&id=" + ",".join(ids[i:i + 150])).get("result", {})
        out.update({k: v for k, v in r.items() if k != "uids"})
    return out


def cat_of(types, title):
    T = set(types)
    if "Practice Guideline" in T or "Guideline" in T: c = "Guideline"
    elif "Consensus Statement" in T: c = "Consensus"
    elif "Meta-Analysis" in T: c = "Meta-analysis"
    elif "Systematic Review" in T: c = "Systematic Review"
    elif "Randomized Controlled Trial" in T: c = "RCT"
    elif any(t.startswith("Clinical Trial") for t in T): c = "Clinical Trial"
    elif "Review" in T: c = "Review"
    elif "Case Reports" in T: c = "Case Report"
    elif T & {"Editorial", "Comment", "Letter"}: c = "Commentary"
    else: c = "Original"
    if c == "Original" and re.search(r"consensus|recommendations|practice parameter|position statement", title, re.I):
        c = "Consensus"
    return c


def ym(a):
    s = (a.get("sortpubdate") or a.get("pubdate") or "")[:7].replace("/", "-")
    return s if re.match(r"\d{4}-\d{2}$", s) else s[:4]


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def main():
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    D = json.load(open(DATA, encoding="utf-8"))
    cutoff = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")

    hits, fails = {}, 0
    for s in D["panels"]:
        term = f"{tiab(s['q'])} AND {IMG} AND {KIND} NOT ({EXCLUDE})"
        try:
            hits[s["id"]] = esearch(term)
            print(f"{s['id']}: {len(hits[s['id']])} hits")
        except RuntimeError as e:
            fails += 1
            print(f"{s['id']}: FAILED {e}", file=sys.stderr)
    if fails == len(D["panels"]):
        sys.exit("All PubMed searches failed; data.json left unchanged.")

    summ = esummary(sorted({i for ids in hits.values() for i in ids}))
    added = removed = 0
    for s in D["panels"]:
        g = s.get("g", [])
        before = len(g)
        # re-running on the same day replaces that day's automatic items; also clears the
        # untagged automatic items from the first (unfiltered) run on 2026-11-23
        g = [x for x in g if not ((x.get("a") and x.get("w") == today) or
                                  (not x.get("a") and not x.get("p") and (x.get("w") or "") >= "2026-11-23"))]
        g = [x for x in g if (x.get("w") or today) >= cutoff]
        removed += before - len(g)
        topic = topic_re(s)
        seen_t = {norm(x["t"]) for x in g}
        seen_u = {x["u"] for x in g}
        new = []
        for pmid in hits.get(s["id"], []):
            a = summ.get(pmid)
            if not a or not a.get("title"):
                continue
            title = re.sub(r"<[^>]+>", "", a["title"]).strip().rstrip(".")
            u = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            if u in seen_u or norm(title) in seen_t:
                continue
            k = cat_of(a.get("pubtype", []), title)
            if NOISE.search(title) or not (k in ("Guideline", "Consensus") or STRONG.search(title)):
                continue
            if not topic.search(re.sub(r"\bjoint (position|statement|guideline|consensus|recommendation)", "", title, flags=re.I)):
                # title must be about this panel's topic ("Joint Position Statement" is not about joints)
                continue
            item = {"w": today, "d": ym(a), "k": k, "o": a.get("source", ""), "t": title, "u": u, "p": "", "a": 1}
            if a.get("source") in IF:
                item["f"], item["fy"] = IF[a["source"]]
            new.append(item)
            seen_u.add(u); seen_t.add(norm(title))
            if len(new) >= PER_PANEL:
                break
        s["g"] = new + g
        added += len(new)

    D["latest"] = today
    D["updated"] = now.strftime("%Y-%m-%d %H:%M")
    json.dump(D, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"done: +{added} new, -{removed} older than {KEEP_DAYS} days")


if __name__ == "__main__":
    main()
