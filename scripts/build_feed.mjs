// 由 GitHub Actions 每天執行：用 NCBI_API_KEY 查 PubMed，產生 feed.json 給網頁讀取。
// 網頁本身不再帶任何 API key。
// 本機測試：NCBI_API_KEY=xxx node scripts/build_feed.mjs
import { readFile, writeFile } from "node:fs/promises";

const KEY = process.env.NCBI_API_KEY || "";
const EU = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/";
const WINS = [7, 30, 90, 365, 0];          // 與網頁「期間」按鈕一致；0 = all
const GAP = KEY ? 120 : 400;              // 有 key 每秒 10 次，沒有 key 每秒 3 次

// 與 index.html 的 loadFeed() 相同的查詢邏輯
const GUIDE = '(guideline[pt] OR practice guideline[pt] OR consensus[ti] OR "appropriateness criteria"[ti] OR recommendation*[ti] OR "reporting and data system"[ti] OR statement[ti])';
const IMG = '(imaging OR radiolog* OR CT OR MRI OR ultrasound OR PET)';
const EX = '(review[pt] OR meta-analysis[pt] OR systematic review[pt] OR guideline[pt] OR practice guideline[pt] OR consensus statement[pt] OR editorial[pt] OR comment[pt] OR letter[pt])';
const jq = arr => "(" + arr.map(j => `"${j}"[ta]`).join(" OR ") + ")";

const sleep = ms => new Promise(r => setTimeout(r, ms));
let last = 0;
async function get(path) {
  const url = EU + path + "&tool=radiology-board" + (KEY ? "&api_key=" + KEY : "");
  for (let attempt = 1; attempt <= 4; attempt++) {
    const wait = last + GAP - Date.now();
    if (wait > 0) await sleep(wait);
    last = Date.now();
    try {
      const r = await fetch(url);
      if (r.status === 429 || r.status >= 500) throw new Error("HTTP " + r.status);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    } catch (e) {
      if (attempt === 4) throw e;
      await sleep(1500 * attempt);
    }
  }
}

const data = JSON.parse(await readFile("data.json", "utf8"));
let prev = { panels: {}, items: {} };
try { prev = JSON.parse(await readFile("feed.json", "utf8")); } catch {}

const out = { updated: new Date().toISOString(), wins: WINS, panels: {}, items: {} };
let ok = 0, failed = [];

for (const s of data.panels) {
  const J = jq([...new Set([...(s.j || []), ...(data.gen || [])])]);
  const Q = [
    ["cg",   `${s.q} AND ${IMG} AND (${GUIDE} OR consensus statement[pt])`],
    ["meta", `${s.q} AND ${IMG} AND (meta-analysis[pt] OR systematic review[pt])`],
    ["rev",  `${s.q} AND ${J} AND review[pt] NOT (meta-analysis[pt] OR systematic review[pt])`],
    ["orig", `${s.q} AND ${J} NOT ${EX}`],
  ];
  try {
    const perWin = {};
    for (const w of WINS) {
      const seen = new Set(), list = [];
      for (const [bk, term] of Q) {
        let p = "esearch.fcgi?db=pubmed&retmode=json&retmax=10&sort=pub_date&term=" + encodeURIComponent(term);
        if (w) p += "&datetype=edat&reldate=" + w;
        const ids = (await get(p)).esearchresult?.idlist || [];
        for (const id of ids) if (!seen.has(id)) { seen.add(id); list.push([id, bk]); }
      }
      perWin[w] = list;
    }
    const need = [...new Set(Object.values(perWin).flat().map(x => x[0]))].filter(id => !out.items[id]);
    for (let i = 0; i < need.length; i += 150) {
      const sm = (await get("esummary.fcgi?db=pubmed&retmode=json&id=" + need.slice(i, i + 150).join(","))).result || {};
      for (const id of need.slice(i, i + 150)) {
        const a = sm[id]; if (!a) continue;
        out.items[id] = {
          t: a.title, j: a.source,
          d: a.sortpubdate?.slice(0, 10) || a.pubdate,
          au: (a.authors?.[0]?.name || "") + (a.authors?.length > 1 ? " et al." : ""),
          pt: (a.pubtype || []).join(", "),
        };
      }
    }
    out.panels[s.id] = perWin;
    ok++;
    console.log(`✓ ${s.id}: ${Object.values(perWin).map(l => l.length).join("/")}`);
  } catch (e) {
    failed.push(s.id);
    console.log(`✗ ${s.id}: ${e.message}（保留上一次的資料）`);
    if (prev.panels?.[s.id]) {
      out.panels[s.id] = prev.panels[s.id];
      for (const id of new Set(Object.values(prev.panels[s.id]).flat().map(x => x[0])))
        if (prev.items?.[id]) out.items[id] = prev.items[id];
    }
  }
}

if (!ok) { console.error("全部面板都失敗，不覆寫 feed.json"); process.exit(1); }
await writeFile("feed.json", JSON.stringify(out));

// ---------- 摘要：另存 abstracts.json（網頁點開「摘要」時才載入），已抓過的不重抓 ----------
async function getText(path) {
  const url = EU + path + "&tool=radiology-board" + (KEY ? "&api_key=" + KEY : "");
  for (let attempt = 1; attempt <= 4; attempt++) {
    const wait = last + GAP - Date.now();
    if (wait > 0) await sleep(wait);
    last = Date.now();
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.text();
    } catch (e) {
      if (attempt === 4) throw e;
      await sleep(1500 * attempt);
    }
  }
}
const unxml = s => s.replace(/<[^>]+>/g, "")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&apos;/g, "'")
  .replace(/&#x([0-9a-f]+);/gi, (_, h) => String.fromCodePoint(parseInt(h, 16)))
  .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(+d)).replace(/&amp;/g, "&")
  .replace(/\s+/g, " ").trim();
function parseAbstracts(xml) {
  const res = {};
  for (const art of xml.split("<PubmedArticle>").slice(1)) {
    const pmid = (art.match(/<PMID[^>]*>(\d+)<\/PMID>/) || [])[1];
    if (!pmid) continue;
    const abs = (art.match(/<Abstract>([\s\S]*?)<\/Abstract>/) || [])[1] || "";
    const parts = [];
    for (const m of abs.matchAll(/<AbstractText([^>]*)>([\s\S]*?)<\/AbstractText>/g)) {
      const txt = unxml(m[2]); if (!txt) continue;
      const label = (m[1].match(/Label="([^"]*)"/) || [])[1] || "";
      parts.push(label && label.toUpperCase() !== "UNLABELLED" ? `${label}: ${txt}` : txt);
    }
    res[pmid] = parts.join("\n");
  }
  return res;
}
let prevAb = {};
try { prevAb = JSON.parse(await readFile("abstracts.json", "utf8")); } catch {}
const ab = {};
for (const id of Object.keys(out.items)) if (id in prevAb) ab[id] = prevAb[id];
const needAb = Object.keys(out.items).filter(id => !(id in ab));
let abFail = 0;
for (let i = 0; i < needAb.length; i += 150) {
  const ids = needAb.slice(i, i + 150);
  try {
    Object.assign(ab, parseAbstracts(await getText("efetch.fcgi?db=pubmed&retmode=xml&id=" + ids.join(","))));
  } catch (e) { abFail += ids.length; console.log(`✗ 摘要 ${i}-${i + ids.length}: ${e.message}（下次再補）`); }
}
await writeFile("abstracts.json", JSON.stringify(ab));
console.log(`摘要：新抓 ${needAb.length - abFail} 篇，共 ${Object.values(ab).filter(Boolean).length} 篇有摘要`);
console.log(`完成：${ok} 個面板成功${failed.length ? "，失敗：" + failed.join(", ") : ""}；共 ${Object.keys(out.items).length} 篇文章`);
