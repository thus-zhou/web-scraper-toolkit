#!/usr/bin/env node
// scrape.js — 通用采集器（静态页 + JS 渲染页都吃），输出 JSON/CSV。
// 用法：
//   node scrape.js --url "https://quotes.toscrape.com/js/" --item ".quote" \
//        --fields "text:.text,author:.author,tags:.tags .tag" --out out.csv
//   node scrape.js --url "https://x.com/list?page={n}" --pages 3 --item ".row" --fields "name:.n,price:.p"
// 说明：--fields 每个字段为 "name:selector"，取不到就给空串；--attr 可指定取属性（默认取文本）。
const { chromium } = require('playwright');

function arg(name, def = null) {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 ? process.argv[i + 1] : def;
}

function csvCell(v) {
  const s = (v ?? '').toString().replace(/\s+/g, ' ').trim();
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

(async () => {
  const urlTpl = arg('url');
  if (!urlTpl) { console.error('need --url'); process.exit(2); }
  const itemSel = arg('item', 'body');
  const pages = parseInt(arg('pages', '1'), 10);
  const out = arg('out', 'out.csv');
  const limit = parseInt(arg('limit', '0'), 10);
  const fields = arg('fields', '').split(',').filter(Boolean).map((f) => {
    const idx = f.indexOf(':');
    return { name: f.slice(0, idx).trim(), sel: f.slice(idx + 1).trim() };
  });
  if (!fields.length) { console.error('need --fields name:selector,...'); process.exit(2); }

  const browser = await chromium.launch({ args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage({
    userAgent: 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0 Safari/537.36',
    locale: 'en-US',
  });
  const rows = [];
  for (let p = 1; p <= pages; p++) {
    const url = urlTpl.replace('{n}', String(p));
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForSelector(itemSel, { timeout: 30000 }).catch(() => {});
    const batch = await page.$$eval(itemSel, (nodes, specs) => nodes.map((n) => {
      const rec = {};
      for (const s of specs) {
        const el = n.querySelector(s.sel);
        rec[s.name] = el ? (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim() : '';
      }
      return rec;
    }), fields);
    rows.push(...batch);
    console.log(`[scrape] page ${p}: ${batch.length} rows`);
    if (limit && rows.length >= limit) { rows.length = limit; break; }
  }
  await browser.close();

  if (out.endsWith('.json')) {
    require('fs').writeFileSync(out, JSON.stringify(rows, null, 2));
  } else {
    const header = fields.map((f) => f.name).join(',');
    const body = rows.map((r) => fields.map((f) => csvCell(r[f.name])).join(',')).join('\n');
    require('fs').writeFileSync(out, header + '\n' + body + '\n');
  }
  console.log(`[scrape] wrote ${rows.length} rows -> ${out}`);
})().catch((e) => { console.error('[scrape] FAIL', e.message); process.exit(1); });
