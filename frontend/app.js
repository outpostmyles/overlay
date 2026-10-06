"use strict";

const state = { snapshot: null, paper: null, tab: "picks", propIndex: {}, dayFilter: null, trackTab: "open", scenario: { pins: [], deltas: [] } };

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (p) => (p == null ? "—" : (p * 100).toFixed(1) + "%");
const sgn = (n) => (n > 0 ? "+" + n : "" + n);
// the break-even American price for a fair probability: a book has to beat this for the bet to have value
const fairAm = (p) => { const q = Math.min(Math.max(p, 0.01), 0.99);
  return q >= 0.5 ? Math.round(-100 * q / (1 - q)) : Math.round(100 * (1 - q) / q); };
const when = (t) => (t ? esc(t) : "");
const ico = (name, cls = "ico") => `<svg class="${cls}"><use href="#i-${name}"/></svg>`;
// XSS-safe minimal markdown: escape FIRST, then convert a whitelist (## headings, **bold**, *em*, breaks)
const mdLite = (s) => esc(s ?? "")
  .replace(/^\s*#{1,6}\s*(.+)$/gm, "<b>$1</b>")
  .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
  .replace(/\*(.+?)\*/g, "<em>$1</em>")
  .replace(/\n{2,}/g, "<br><br>").replace(/\n/g, "<br>");
const fmtPop = (n) => { n = n || 0; return Math.abs(n) >= 1000 ? (n / 1000).toFixed(1) + "k" : "" + n; };
const dateLabel = (d) => (d == null ? "" : d === 0 ? "Today" : d === 1 ? "Tomorrow" : "in " + d + "d");

async function getJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ---------- data ----------
async function loadSnapshot(force = false, refreshOdds = false, reason = false) {
  $("#meta-updated").textContent = reason ? "running AI analysis…" : refreshOdds ? "fetching sportsbook lines…" : "loading…";
  try {
    const qs = [];
    if (force) qs.push("force=true");
    if (refreshOdds) qs.push("refresh_odds=true");
    if (reason) qs.push("reason=true");
    state.snapshot = await getJSON("api/snapshot" + (qs.length ? "?" + qs.join("&") : ""));
    renderAll();
  } catch (e) {
    $("#meta-updated").textContent = "error: " + e.message.slice(0, 80);
  }
}
async function loadPaper() {
  state.paper = await getJSON("api/paper");
  renderPaper();
}

// ---------- render: top-level ----------
// Ledger explainer for the anchor-only sports (no model): the sharp market line IS the forecast.
// The World Cup keeps the default text in index.html, which describes its model-vs-market sheet.
// a tab's intro: one plain sentence, with the method folded under "How this works"
const _hint = (lead, more) => `${lead} <details class="how"><summary>How this works</summary><div>${more}</div></details>`;
const ANCHOR_HINTS = {
  mlb: _hint(`Every game's line, frozen about 75 minutes before first pitch and graded off the final: the de-vigged market's moneyline, total, first five innings and key player props.`, `A pre-first-pitch <b>prediction sheet</b> for every game, frozen ~75 minutes before start: the de-vigged market's call on the moneyline plus the main total, the first-5-innings result, and the game's most competitive player props. Every line locks, then grades off the free box score (props void on a DNP; rainouts void, never lose). No model rides here by design: the sharp market line IS the forecast, and the ledger measures how well it is calibrated, including whether the locked line loses information to the close.`),
  nfl: _hint(`Every game's line, frozen about 75 minutes before kickoff (after inactives) and graded off the final: the de-vigged market's moneyline, spread, total and the props that matter, with the research beside it.`, `A pre-kickoff <b>prediction sheet</b> for every game, frozen ~75 minutes before kickoff (after inactives are announced): the de-vigged market's call on the moneyline, the main spread and total, and a prop sheet built from the five props that matter (passing, rushing and receiving yards, receptions, and anytime TD): the starting QBs, the lead backs, the featured receivers, and the likeliest scorers. Everything grades off the free ESPN box score. A player who never takes a snap voids, the way Kalshi settles him; one snap and he is graded on what he recorded, zero included. Beside it sits the <b>research</b>: what betting research says about the game, checked against every NFL closing line since 1999. Only wind and rain move a number (the total); the moneyline stays the market's, because 25 years of closes showed it priced right on average. Everything else (heavy favorites, QB status, rest, travel, the top bettors' side) is shown and graded in the Research study below, so the record shows what deserves weight.`),
  cfb: _hint(`Every Power 4 and Notre Dame game's line, frozen about 75 minutes before kickoff and graded off the final: the de-vigged market's moneyline, spread and total, with the research beside it.`, `A pre-kickoff <b>prediction sheet</b> for every Power 4 and Notre Dame game, frozen ~75 minutes before kickoff: the de-vigged market's call on the moneyline, the main spread, and the main total, graded off the free ESPN final. A game whose Kalshi book is still thin (wider than 10 cents) waits until it tightens, because a thin book is not a sharp line. Beside it sits the <b>research</b>: what betting research says about the game. Nothing moves a college number yet (the NFL test found the moneyline priced right, and college weather is graded first); heavy favorites, weather, big spreads, rest and the top bettors' side are shown and graded in the Research study below before they earn weight.`),
  nhl: _hint(`Every game's line, frozen about 75 minutes before puck drop and graded off the final: the de-vigged market's moneyline and total, with the research beside it.`, `A pre-puck-drop <b>prediction sheet</b> for every game, frozen ~75 minutes before start: the de-vigged market's call on the moneyline plus the main total. Both lock, then grade off the free ESPN final, with overtime and shootouts counted exactly as the market settles them (a shootout winner is credited one goal). No model rides here by design: the sharp market line IS the forecast, and the ledger measures how well it is calibrated, including whether the locked line loses information to the close.`),
};

const RESEARCH_HINTS = {
  nfl: _hint("What the research says about this week's games, how each factor has done against the market, and the de-vigged Kalshi price on every player prop.",
    "A factor <b>adjusts</b> the Research % only where 25 years of NFL closing lines show the market misprices it (today: wind and rain on totals). Everything else is <b>tracked</b>: shown on the game, graded, and left out of the number until it earns its place. Player props are Kalshi's prices with the vig removed: hold your book's price on the same line against the fair odds."),
  cfb: _hint("What the research says about this week's games and how each factor has done against the market.",
    "Nothing moves a college number yet: heavy favorites, weather, big spreads, rest and the top bettors' side are shown on each game and graded against the market's price before they earn weight."),
  nhl: _hint("What the research says about each game (heavy favorites, back-to-backs, the top bettors' side) and how each has done against the market.",
    "Nothing moves a hockey number yet: every factor is tracked, shown on the game and graded against the market's price before it earns weight."),
  mlb: _hint("The de-vigged Kalshi price on every player prop line, and the market's clear favorites.",
    "Kalshi's prices with the vig removed are the sharp fair probability on each line. Hold your book's price on the same line against the fair odds: value only exists where the book pays more."),
};
const _BOARD_NAMES = { nfl: "NFL", cfb: "College Football", nhl: "NHL", mlb: "MLB", wc: "World Cup 2026" };
// name the board from its address before the data arrives, so it never flashes as another sport
(() => { const seg = location.pathname.split("/")[1]; const n = _BOARD_NAMES[seg];
  if (n) { const b = document.getElementById("sport-badge"); if (b) b.textContent = n; document.title = `Overlay · ${n}`; } })();
// "updated 4:21 PM": the snapshot's UTC stamp in the viewer's own time, like every kickoff on the page
const _updatedLabel = (s) => { const d = new Date(s.generated_iso || "");
  return isNaN(d) ? s.generated_at : d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }); };
const _showTab = (name, on) => { const b = document.querySelector(`.tab[data-tab="${name}"]`); if (b) b.style.display = on ? "" : "none"; };

function renderAll() {
  const s = state.snapshot; if (!s) return;
  const m = s.meta;
  const pk = s.picks || {};
  $("#meta-updated").textContent = "updated " + _updatedLabel(s);
  // the sources this board actually reads: DraftKings arrives free through ESPN, and Polymarket's
  // wallets feed Top Bettors even where its prices do not set the line; no model, no Model pill
  const dkLive = (pk.best_bets || []).some((c) => c.dk_price != null)
    || ((pk.model_ledger || {}).upcoming || []).some((r) => r.research && r.research.dk);
  const pmLive = m.sources_live.polymarket || !!(((pk.top_traders || {}).meta || {}).poly_traders);
  setPill("pill-pm", pmLive);
  setPill("pill-kalshi", m.sources_live.kalshi);
  setPill("pill-books", m.sources_live.sportsbooks || dkLive);
  $("#pill-books").textContent = m.sources_live.sportsbooks ? "Sportsbooks" : "DraftKings";
  setPill("pill-model", m.model_loaded);
  $("#pill-model").style.display = m.model_loaded ? "" : "none";
  // controls that need a key the public site does not have stay out of the way
  $("#refresh-ai").style.display = m.ai_enabled ? "" : "none";

  // sportsbook odds credit meter (rail footer)
  const od = m.odds || {};
  const oc = $("#odds-credits"), btn = $("#refresh-odds"), meter = $("#odds-meter"), fill = $("#odds-meter-fill");
  document.querySelector(".rail-foot").style.display = od.enabled ? "" : "none";
  if (!od.enabled) {
    oc.textContent = "no API key";
    oc.classList.remove("low"); meter.classList.remove("low"); fill.style.width = "0";
    btn.disabled = true;
  } else {
    const left = od.credits_remaining;
    oc.textContent = left != null ? `${left} / 500 credits` : "key set · tap Odds";
    const low = left != null && left <= 25;
    oc.classList.toggle("low", low); meter.classList.toggle("low", low);
    fill.style.width = (left != null ? Math.max(2, Math.min(100, (left / 500) * 100)) : 0) + "%";
    btn.disabled = false;
  }
  // tabs a board cannot fill stay hidden: futures need a bracket; the finished World Cup has no live
  // lines to bet, parlay or research; Track Record only fills where the AI logs picks
  const caps = (s.meta && s.meta.capabilities) || [];
  const archived = !!m.archived;
  _showTab("futures", caps.includes("futures"));
  ["slip", "lotto", "research"].forEach((t) => _showTab(t, !archived));
  _showTab("top", !archived && !!pk.top_traders);
  _showTab("track", !!m.ai_enabled || (m.paper_picks || 0) > 0);
  const picksLabel = document.querySelector('.tab[data-tab="picks"] .label');
  if (picksLabel) picksLabel.textContent = archived ? "Final results" : "Best Bets";
  const activeBtn = document.querySelector(`.tab[data-tab="${state.tab}"]`);
  if (activeBtn && activeBtn.style.display === "none") switchTab("picks");
  renderBoards(s.meta);
  // the header names the active sport (the app is multi-sport now)
  if (s.meta && s.meta.sport_name) {
    const badge = document.getElementById("sport-badge");
    if (badge) badge.textContent = s.meta.sport_name;
    document.title = `Overlay · ${s.meta.sport_name}`;
    [["#tab-ledger .hint", ANCHOR_HINTS], ["#tab-research .hint", RESEARCH_HINTS]].forEach(([sel, hints]) => {
      const hint = document.querySelector(sel), text = hints[s.meta.sport];
      if (hint && text && hint.dataset.sport !== s.meta.sport) { hint.dataset.sport = s.meta.sport; hint.innerHTML = text; }
    });
  }
  renderPicks();
  renderLedger();
  renderSlip();
  renderLotto();
  renderTopBettors();
  renderResearch();
  renderFutures();
  if (state.tab === "track") loadPaper();
}

// ESPN's own team names when the board ships them ("Texas A&M", "BYU"), else a title-cased key
const teamName = (k) => ((state.snapshot && state.snapshot.picks && state.snapshot.picks.team_names) || {})[k]
  || (k || "").replace(/\b\w/g, (c) => c.toUpperCase());

function scoutNotes() { return localStorage.getItem("overlay_futures_notes") || ""; }

function renderLeans(leans) {
  if (!leans || !leans.length) return "";
  const row = (l) => {
    const settled = l.status === "won" || l.status === "lost";
    const badge = l.status === "won" ? ` <span class="tag won">WON</span>` : l.status === "lost" ? ` <span class="tag lost">LOST</span>` : "";
    const metric = settled ? l.realized_clv : l.drift_pp;
    const cls = metric == null ? "muted" : metric > 0 ? "flow-up" : metric < 0 ? "flow-dn" : "muted";
    return `<tr class="${settled ? "lean-settled" : ""}"><td><b>${esc(teamName(l.team))}</b> <span class="tag">${esc(l.direction)}</span> <span class="muted">${esc(l.kind)}</span>${badge}${l.note ? `<div class="lean-note">“${esc(l.note)}”</div>` : ""}</td>
      <td class="num">${l.entry_pct}%</td>
      <td class="num">${l.current_pct == null ? "—" : l.current_pct + "%"}</td>
      <td class="num ${cls}">${metric == null ? "—" : (metric > 0 ? "+" : "") + metric + "pp"}</td>
      <td>${settled ? "" : `<button class="act tiny" data-lean-rm="${esc(l.id)}" title="remove this lean">×</button>`}</td></tr>`;
  };
  const done = leans.filter((l) => l.status === "won" || l.status === "lost");
  const w = done.filter((l) => l.status === "won").length;
  const rec = done.length ? ` · settled ${w}-${done.length - w}` : "";
  return `<div class="pick-section"><h3>${ico("track")} Your leans <span class="muted">· live <b>Drift</b> is the sharp line moving toward your call (up for a back, down for a fade); once a stage is decided the lean settles W/L with its realized <b>CLV</b>${rec}</span></h3>
    <table><thead><tr><th>Lean</th><th class="num">Entry</th><th class="num">Now / close</th><th class="num">Drift / CLV</th><th></th></tr></thead>
    <tbody>${leans.map(row).join("")}</tbody></table></div>`;
}

// ---------- render: Model Ledger (pre-kickoff 1X2 forecasts, model vs market, graded) ----------
const _koLabel = (iso, tbd) => {
  if (!iso) return "TBD";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso.slice(0, 16));
  if (tbd) return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" }) + ", time TBD";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " +
    d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
};
// a 2-way sport (no draw) shows "62 / 38"; soccer keeps its three outcomes
const _split = (p) => { const a = Math.round(p[0] * 100);
  if (!p[1]) return [a, 0, 100 - a];
  const d = Math.round(p[1] * 100); return [a, d, 100 - a - d]; };
const _trip = (p) => { const [a, d, b] = _split(p); return p[1] ? `${a} / ${d} / ${b}` : `${a} / ${b}`; };
const _legHead = (a, b, p) => `<div class="fcard-leg muted"><span>${esc(teamName(a))}</span>${p && p[1] ? "<span>Draw</span>" : ""}<span>${esc(teamName(b))}</span></div>`;
// stacked 1X2 bar: team-A win | draw | team-B win
function _triBar(p, a, b) {
  const w = (x) => (Math.max(0, x) * 100).toFixed(1);
  return `<span class="tribar">`
    + `<i class="t-a" style="width:${w(p[0])}%" title="${esc(teamName(a))} win ${Math.round(p[0] * 100)}%"></i>`
    + `<i class="t-d" style="width:${w(p[1])}%" title="draw ${Math.round(p[1] * 100)}%"></i>`
    + `<i class="t-b" style="width:${w(p[2])}%" title="${esc(teamName(b))} win ${Math.round(p[2] * 100)}%"></i></span>`;
}
// what a game total counts, per sport (soccer and hockey count goals)
const _TOTAL_UNIT = { mlb: "runs", nfl: "points", cfb: "points" };
const _totalUnit = () => _TOTAL_UNIT[((state.snapshot || {}).meta || {}).sport] || "goals";
// the model's explicit best guess on one extra market, as a labeled row
const _isAnytimeTd = (l) => l.stat === "touchdowns" && l.line === 0.5;
const _predName = (l) => l.key === "total_goals" ? `Total ${_totalUnit()}`
  : l.key === "team_total" ? `${teamName(l.team)} goals`
  : l.key === "btts" ? "Both teams score"
  : l.key === "spread" ? "Spread"
  : l.key === "player_prop" && _isAnytimeTd(l) ? `${l.player || ""} to score a TD`.trim()
  : l.key === "player_prop" ? `${l.player || ""} ${l.stat || ""}`.trim()
  : l.key === "f5" ? "First 5 innings"
  : l.key === "corners" ? "Corners" : l.key;
const _predPick = (l) => l.key === "btts" ? (l.side === "yes" ? "Yes" : "No")
  : l.key === "spread" ? (l.side === "cover" ? `${teamName(l.team)} -${l.line}` : `${teamName(l.opp)} +${l.line}`)
  : l.key === "player_prop" && _isAnytimeTd(l) ? (l.side === "over" ? "Yes" : "No")
  : l.key === "player_prop" ? (l.side === "over" ? `${Math.ceil(l.line)}+` : `Under ${Math.ceil(l.line)}`)
  : l.key === "f5" ? (l.side === "tie" ? "Tie" : teamName(l.side))
  : `${l.side === "over" ? "Over" : "Under"} ${l.line}`;
// our guess = the projected number (the best estimate) + the resulting pick, e.g. "2.6 · Under 2.5"
const _predGuess = (l) => (l.proj != null ? `${l.proj} · ` : "") + _predPick(l);
const _predActual = (l) => l.result === "pending" ? "awaiting"
  : l.key === "btts" ? (l.actual === "yes" ? "Yes" : "No")
  : l.key === "spread" ? (l.actual > 0 ? `${teamName(l.team)} by ${l.actual}`
                          : l.actual < 0 ? `${teamName(l.opp)} by ${-l.actual}` : "Tie")
  : l.result === "void" ? "void (did not play)"
  : l.key === "f5" ? (l.actual === "tie" ? "Tie" : teamName(l.actual))
  : String(l.actual);
function _predRowsInner(legs, graded) {
  return (legs || []).map((l) => {
    const name = `<span class="pred-l">${esc(_predName(l))}</span>`;
    const perfDiff = l.perf_side && l.perf_side !== l.side;   // show the variant only when it disagrees
    if (!graded) {
      const perf = perfDiff ? ` <span class="perf-tag" title="performance-aware variant, graded forward">perf ${l.perf_side === "over" ? "O" : "U"}${l.line}</span>` : "";
      if (l.research_prob != null) {
        // football: the market %, the Research % when the research moved it, and the price it needs
        const moved = Math.abs(l.research_prob - (l.prob || 0)) >= 0.005;
        const pctTxt = moved ? `${Math.round((l.prob || 0) * 100)}→${Math.round(l.research_prob * 100)}%` : `${Math.round((l.prob || 0) * 100)}%`;
        // the weather and big-spread flags on a line; the every-passing-prop under test stays in the study
        const tagName = (k) => k.startsWith("wind") ? "wind" : k === "pass_weather" ? "weather" : k === "big_spread" ? "big spread" : k === "rain" ? "rain" : null;
        const shown = [...new Set((l.factors || []).map(tagName).filter(Boolean))];
        const tags = shown.length ? ` <span class="rs-tag" title="research factors on this line">${esc(shown.join(", "))}</span>` : "";
        return `<div class="pred pred4">${name}<span class="pred-g">${esc(_predGuess(l))}${perf}${tags}</span><span class="pred-c${moved ? " rs-moved" : ""}" title="market${moved ? " → research" : ""}">${pctTxt}</span><span class="pred-v" title="value at this price or better">${_amer(l.value_at)}</span></div>`;
      }
      return `<div class="pred">${name}<span class="pred-g">${esc(_predGuess(l))}${perf}</span><span class="pred-c">${Math.round((l.prob || 0) * 100)}%</span></div>`;
    }
    const r = l.result;
    const mark = r === "won" ? '<span class="pred-ok">✓</span>' : r === "lost" ? '<span class="pred-no">✗</span>' : '<span class="muted">·</span>';
    const pm = (perfDiff && l.perf_result) ? ` <span class="perf-tag" title="perf-aware variant">perf ${l.perf_result === "won" ? "✓" : l.perf_result === "lost" ? "✗" : "·"}</span>` : "";
    return `<div class="pred ${r === "won" ? "pred-won" : r === "lost" ? "pred-lost" : ""}">${name}<span class="pred-g">${esc(_predPick(l))}</span><span class="pred-a">${esc(_predActual(l))} ${mark}${pm}</span></div>`;
  }).join("");
}
// forward scoreboard: goals-only model vs the perf-aware variant on the goals legs that carry both
function _perfScore(settled, perf) {
  let go = 0, gn = 0, po = 0;
  if (perf) { go = perf.base; gn = perf.n; po = perf.perf; }
  else settled.forEach((r) => (r.legs || []).forEach((l) => {
    if (l.perf_result && (l.result === "won" || l.result === "lost") && (l.key === "total_goals" || l.key === "team_total")) {
      gn++; if (l.result === "won") go++; if (l.perf_result === "won") po++;
    }
  }));
  if (gn < 1) return "";
  return `<div class="cal-note muted"><span>Model variant test (goals legs): goals-only <b>${go}/${gn}</b> vs perf-aware <b>${po}/${gn}</b>. A forward-graded experiment (finishing regressed toward shot volume); not yet conclusive.</span></div>`;
}
// per-prop prediction list inside a card: upcoming shows our guess + confidence; settled shows pick vs actual
function _predRows(legs, graded) {
  if (!legs || !legs.length) return "";
  return `<div class="preds">${_predRowsInner(legs, graded)}</div>`;
}
// the 1X2 as a prediction row for a settled game: the called winner vs what happened. Model sports
// grade the model's call; anchor-only sports grade the market favorite (the ledger IS the market).
function _matchResultRow(r) {
  const hasModel = r.model_a != null;
  const src = hasModel ? [r.model_a, r.model_draw, r.model_b] : [r.market_a, r.market_draw || 0, r.market_b];
  const pick = src.indexOf(Math.max(...src));
  const called = pick === 1 ? "Draw" : teamName(pick === 0 ? r.team_a : r.team_b);
  const actual = r.actual_outcome === "a" ? teamName(r.team_a) : r.actual_outcome === "b" ? teamName(r.team_b) : "Draw";
  const hit = hasModel ? !!r.hit_model
    : (pick === 0 && r.actual_outcome === "a") || (pick === 2 && r.actual_outcome === "b");
  const mark = hit ? '<span class="pred-ok">✓</span>' : '<span class="pred-no">✗</span>';
  const label = hasModel ? "Match result" : "Market favorite";
  return `<div class="pred pred-result ${hit ? "pred-won" : "pred-lost"}"><span class="pred-l">${label}</span><span class="pred-g">${esc(called)}</span><span class="pred-a">${esc(actual)} ${mark}</span></div>`;
}
// per-market hit rate across settled games (only counts legs that actually graded)
function _legAccuracy(settled, tally0) {
  const names = { total_goals: _totalUnit() === "goals" ? "Total goals" : "Totals", team_total: "Team totals",
                  btts: "BTTS", corners: "Corners", player_prop: "Player props", f5: "First 5",
                  spread: "Spreads" };
  const tally = {};
  if (tally0) Object.entries(tally0).forEach(([k, v]) => { tally[k] = { w: v[0], n: v[1] }; });
  else settled.forEach((r) => (r.legs || []).forEach((l) => {
    if (l.result !== "won" && l.result !== "lost") return;
    const t = tally[l.key] || (tally[l.key] = { w: 0, n: 0 });
    t.n++; if (l.result === "won") t.w++;
  }));
  const cells = ["spread", "total_goals", "team_total", "btts", "corners", "player_prop", "f5"].filter((k) => tally[k])
    .map((k) => `<span class="leg-acc"><b>${names[k]}</b> ${tally[k].w}/${tally[k].n}</span>`);
  return cells.length ? `<div class="cal-note muted"><span>Our picks vs result: ${cells.join(" · ")}</span></div>` : "";
}
// ---------- research layer (football): the market line plus the research, as a graded Research % ----------
const _amer = (n) => (n == null ? "-" : (n > 0 ? "+" + n : "" + n));
const _evTxt = (ev) => (ev == null ? "" : `${ev > 0 ? "+" : ""}${(ev * 100).toFixed(1)}%`);
function _rsChips(factors) {
  const game = (factors || []).filter((f) => f.scope !== "leg");
  if (!game.length) return "";
  const cls = { adjust: "adj", track: "trk", caution: "cau" };
  return `<div class="rchips">${game.map((f) => `<span class="rchip ${cls[f.kind] || ""}" title="${esc(f.detail || "")}">${esc(f.label)}</span>`).join("")}</div>`;
}
function _rsNotes(factors) {
  const game = (factors || []).filter((f) => f.scope !== "leg");
  if (!game.length) return "";
  const what = { adjust: "moves the Research %", track: "tracked in the study", caution: "raises the cushion" };
  return `<details class="rs-notes"><summary>${ico("chevron", "ico ico-chev")} why</summary>${game.map((f) =>
    `<div class="rs-note"><b>${esc(f.name || f.label)}</b> <span class="muted">· ${what[f.kind] || ""}</span><div>${esc(f.detail || "")}${f.source ? ` <a href="${esc(f.source)}" target="_blank" rel="noopener">source</a>` : ""}</div></div>`).join("")}</details>`;
}
// the Research row, the price each side needs, and DraftKings' price scored against Research
function _researchBlock(rs, a, b, frozen, market) {
  if (!rs || !rs.probs || rs.probs[0] == null) return "";
  const p = rs.probs, va = rs.value_at || {}, cush = Math.round(((rs.cushion || {}).game || 0.02) * 100);
  // the moneyline only gets its own Research row when the research moved it off the market
  const moved = !market || [0, 1, 2].some((i) => Math.abs((p[i] || 0) - (market[i] || 0)) >= 0.005);
  const row = moved ? `<div class="fcard-row"><span class="fcard-t">Research</span>${_triBar([p[0], p[1] || 0, p[2]], a, b)}<span class="fcard-n">${_trip([p[0], p[1] || 0, p[2]])}</span></div>` : "";
  const value = `<div class="rs-line muted" title="the worst price that still clears a ${cush}% edge over the Research %">value at <b>${esc(teamName(a))} ${_amer(va[a])}</b> · <b>${esc(teamName(b))} ${_amer(va[b])}</b> <span>or better</span></div>`;
  const dk = rs.dk && rs.dk.ml && Object.keys(rs.dk.ml).length ? (() => {
    const side = (t) => {
      const ev = (rs.dk.ev || {})[t];
      const cls = ev == null ? "" : ev * 100 >= cush ? "pos" : ev < 0 ? "neg" : "";
      return `${esc(teamName(t))} <b>${_amer(rs.dk.ml[t])}</b> <span class="ev ${cls}">${_evTxt(ev)}</span>`;
    };
    return `<div class="rs-line" title="${esc(rs.dk.book || "DraftKings")} via ESPN, scored against the Research %${frozen ? " at the lock" : ""}">${esc(rs.dk.book || "DraftKings")}: ${side(a)} · ${side(b)}</div>`;
  })() : "";
  return row + value + dk + _rsChips(rs.factors) + _rsNotes(rs.factors);
}
// a locked or settled ledger row's frozen research, in the same shape the live board sends
function _rowResearch(r) {
  if (!r.research || r.research_a == null) return null;
  return { ...r.research, probs: [r.research_a, r.research_draw || 0, r.research_b] };
}

function _forecastCard(c) {
  const badge = c.postponed ? `<span class="tag" title="ESPN lists this game as postponed or canceled; it voids">postponed</span>`
    : c.frozen
    ? `<span class="tag pin" title="frozen ${esc(c.lock_ts || "")}">locked</span>`
    : `<span class="tag" title="live line, not yet part of the record">preview</span>`;
  const hasModel = c.model && c.model[0] != null;   // anchor-only sports lock the market line alone
  // biggest model-vs-market disagreement across the three outcomes, flagged when it is material
  const gap = hasModel ? Math.round(Math.max(...[0, 1, 2].map((i) => Math.abs(c.model[i] - c.market[i]))) * 100) : 0;
  const gapChip = gap >= 8 ? ` <span class="tag gap" title="largest model vs market gap">Δ${gap}pp</span>` : "";
  const modelRow = hasModel
    ? `<div class="fcard-row"><span class="fcard-t">Model</span>${_triBar(c.model, c.a, c.b)}<span class="fcard-n">${_trip(c.model)}</span></div>` : "";
  return `<div class="fcard">
    <div class="fcard-h"><span class="fcard-m"><b>${esc(teamName(c.a))}</b> <span class="muted">v</span> <b>${esc(teamName(c.b))}</b></span><span class="fcard-k muted">${_koLabel(c.kickoff_iso, c.time_tbd)} ${badge}${gapChip}</span></div>
    ${_legHead(c.a, c.b, c.market)}
    ${modelRow}
    <div class="fcard-row"><span class="fcard-t">Market</span>${_triBar(c.market, c.a, c.b)}<span class="fcard-n">${_trip(c.market)}</span></div>
    ${_researchBlock(c.research, c.a, c.b, c.frozen, c.market)}
    ${_predRows(c.legs, false)}
  </div>`;
}
// settled game recap: the model line, our called result, then every prop predicted vs actual
function _settledCard(r) {
  const hasModel = r.model_a != null;
  const score = `${r.actual_a}-${r.actual_b}${r.pens ? " (pens)" : ""}`;
  const lineRow = hasModel
    ? `<div class="fcard-row"><span class="fcard-t">Model</span>${_triBar([r.model_a, r.model_draw, r.model_b], r.team_a, r.team_b)}<span class="fcard-n">${_trip([r.model_a, r.model_draw, r.model_b])}</span></div>`
    : `<div class="fcard-row"><span class="fcard-t">Market</span>${_triBar([r.market_a, r.market_draw || 0, r.market_b], r.team_a, r.team_b)}<span class="fcard-n">${_trip([r.market_a, r.market_draw || 0, r.market_b])}</span></div>`;
  const brier = (hasModel && r.brier_model != null && r.brier_market != null)
    ? `<div class="fcard-brier muted" title="model 1X2 Brier vs the market frozen at lock (lower is better)">1X2 Brier ${r.brier_model.toFixed(2)} <span class="${r.brier_model < r.brier_market ? "pos" : "neg"}">vs market ${r.brier_market.toFixed(2)}</span></div>`
    : (r.brier_market != null
      ? `<div class="fcard-brier muted" title="the de-vigged market line's Brier on this game (lower is better)">Market Brier ${r.brier_market.toFixed(2)}${r.closing_a != null ? ` · closed ${_trip([r.closing_a, r.closing_draw || 0, r.closing_b]).replace(/ /g, "")}` : ""}</div>` : "");
  const mktRow = hasModel && r.market_a != null
    ? `<div class="fcard-row"><span class="fcard-t">Market</span>${_triBar([r.market_a, r.market_draw || 0, r.market_b], r.team_a, r.team_b)}<span class="fcard-n">${_trip([r.market_a, r.market_draw || 0, r.market_b])}</span></div>` : "";
  const rs = _rowResearch(r);
  const rsMoved = rs && [0, 1, 2].some((i) => Math.abs((rs.probs[i] || 0) - ([r.market_a, r.market_draw || 0, r.market_b][i] || 0)) >= 0.005);
  const rsRow = rsMoved ? `<div class="fcard-row"><span class="fcard-t">Research</span>${_triBar(rs.probs, r.team_a, r.team_b)}<span class="fcard-n">${_trip(rs.probs)}</span></div>` : "";
  const rsBrier = (rsMoved && r.brier_research != null && r.brier_market != null)
    ? `<div class="fcard-brier muted" title="the Research % Brier on this game vs the market's, frozen at the same lock (lower is better)">Research Brier ${r.brier_research.toFixed(3)} <span class="${r.brier_research < r.brier_market ? "pos" : r.brier_research > r.brier_market ? "neg" : ""}">vs market ${r.brier_market.toFixed(3)}</span></div>` : "";
  return `<div class="fcard settled">
    <div class="fcard-h"><span class="fcard-m"><b>${esc(teamName(r.team_a))}</b> <span class="muted">v</span> <b>${esc(teamName(r.team_b))}</b></span><span class="fcard-k"><b>${esc(score)}</b> <span class="muted">${esc((r.commence_time || "").slice(0, 10))}</span></span></div>
    ${_legHead(r.team_a, r.team_b, [r.market_a, r.market_draw, r.market_b])}
    ${lineRow}${mktRow}${rsRow}
    ${rs ? _rsChips(rs.factors) : ""}
    <div class="preds">${_matchResultRow(r)}${_predRowsInner(r.legs, true)}</div>
    ${brier}${rsBrier}
  </div>`;
}
function _ledgerScore(s) {
  const open = s.locked_pending ? `<span class="muted">${s.locked_pending} locked, awaiting result</span>` : "";
  if (!s.ready) {
    // anchor-only sports have no model column; the market's own calibration is the scoreboard
    if (s.market_n >= s.min_n && s.market_brier != null) {
      const closeStat = (s.close_brier != null)
        ? `<div class="stat"><div class="label">Lock vs close Brier</div><div class="val">${s.lock_brier_on_closed} <span class="muted" style="font-size:13px">vs ${s.close_brier}</span></div></div>` : "";
      return `<div class="summary lg-score">
        <div class="stat"><div class="label">Market Brier <span class="muted" title="summed over the outcomes: 0 is perfect; a coin-flip line on a two-way game scores 0.50">· coin flip 0.50</span></div><div class="val">${s.market_brier}</div></div>
        <div class="stat"><div class="label">Market favorite hit rate</div><div class="val">${s.market_hit_rate}%</div></div>
        ${closeStat}
        <div class="stat"><div class="label">Graded games</div><div class="val">${s.market_n}</div></div>
      </div>
      <div class="cal-note muted"><span>Anchor-only ledger: the de-vigged market line locks before each game and grades itself. No model rides here by design. ${open}</span></div>`;
    }
    return `<div class="cal-note">${ico("track")}<span><b>${s.market_n != null && s.n === 0 ? s.market_n : s.n}</b> of ${s.min_n} graded games so far. The scorecard appears once ${s.min_n} settle, so a couple of games cannot pass for a verdict; each game below is graded on its own. ${open}</span></div>`;
  }
  const skill = s.skill_vs_market;
  const scls = skill == null ? "" : skill > 0 ? "pos" : "neg";
  const verdict = skill == null ? "even with the market"
    : skill > 0 ? "model is beating the market" : "model trails the market";
  return `<div class="summary lg-score">
    <div class="stat"><div class="label">Brier skill vs market</div><div class="val ${scls}">${skill == null ? "—" : (skill > 0 ? "+" : "") + skill + "%"}</div></div>
    <div class="stat"><div class="label">Model Brier</div><div class="val">${s.brier_model} <span class="muted" style="font-size:13px">vs ${s.brier_market}</span></div></div>
    <div class="stat"><div class="label">Winner hit rate</div><div class="val">${s.hit_rate}%</div></div>
    <div class="stat"><div class="label">Model closer than market</div><div class="val">${s.beat_market}/${s.n}</div></div>
  </div>
  <div class="cal-note muted"><span>${esc(verdict)}. Exploratory: ${s.n} settled is a small sample with wide error bars. This measures the model, it is not a betting record, and it must never be used to retune the model. ${open}</span></div>`;
}
// how the market's favorite has done at each price, pooled across every sport in the ledger
function _favPriceHTML(fp) {
  if (!fp || !fp.n) return "";
  const pct = (v) => (v != null ? v.toFixed(1) + "%" : "-");
  const row = (b) => `<tr>
    <td><b>${esc(b.label)}</b></td>
    <td class="num">${b.n}</td>
    <td class="num">${pct(b.said)}</td>
    <td class="num">${pct(b.actual)}</td>
    <td class="num">${b.n ? `${b.won} of ${b.n}` : "-"}</td></tr>`;
  const h = fp.heavy;
  const note = h && h.n
    ? `Favorites at -233 or shorter have won <b>${h.won} of ${h.n}</b> against ${h.expected} expected. That pattern was spotted in the data before it was tested, so it is something to watch, not a proven edge; the football seasons will add hundreds of these games.`
    : "";
  return `<div class="pick-section"><h3>${ico("track")} Favorites by price <span class="muted">· every sport in the ledger, pooled (${fp.n} games)</span></h3>
    <div class="tbl-scroll"><table class="flat"><thead><tr><th>Favorite's price</th><th class="num">Games</th><th class="num">Market said</th><th class="num">Actually won</th><th class="num">Record</th></tr></thead>
    <tbody>${fp.bands.map(row).join("")}</tbody></table></div>
    ${note ? `<div class="cal-note">${ico("shield")} <span>${note}</span></div>` : ""}</div>`;
}

// the research layer's report card: Research % vs the market, then every factor's record against its price
function _researchStudyHTML(st, cat) {
  if (!st && !cat) return "";
  const sport = ((state.snapshot || {}).meta || {}).sport;
  const football = sport === "nfl" || sport === "cfb";
  const n = st ? st.ml.n : 0, minN = (st && st.min_n) || 8;
  const pct = (v) => (v != null ? v.toFixed(1) + "%" : "-");
  const mode = { adjusts: "adjusts", tracks: "tracks", caution: "cushion" };
  let head;
  if (n >= minN) {
    const sk = st.ml.skill, cls = sk == null ? "" : sk > 0 ? "pos" : sk < 0 ? "neg" : "";
    const legs = st.legs.n ? `<div class="stat"><div class="label">Lines it moved</div><div class="val">${st.legs.brier_research} <span class="muted" style="font-size:13px">vs ${st.legs.brier_market} · ${st.legs.n}</span></div></div>` : "";
    head = `<div class="summary lg-score">
      <div class="stat"><div class="label">Research vs market</div><div class="val ${cls}">${sk == null ? "-" : (sk > 0 ? "+" : "") + sk + "%"}</div></div>
      <div class="stat"><div class="label">Moneyline Brier</div><div class="val">${st.ml.brier_research} <span class="muted" style="font-size:13px">vs ${st.ml.brier_market}</span></div></div>
      <div class="stat"><div class="label">Research closer</div><div class="val">${st.ml.closer}/${n}</div></div>${legs}
    </div>`;
  } else {
    head = `<div class="cal-note muted">${ico("track")} <span><b>${n}</b> of ${minN} graded games so far. The scorecard appears once ${minN} settle; each factor below fills in as games grade.</span></div>`;
  }
  const rows = (st && st.factors) || [];
  const tbl = rows.length ? `<div class="tbl-scroll"><table class="flat"><thead><tr><th>Factor</th><th class="num">Graded</th><th class="num">Market said</th><th class="num">Research said</th><th class="num">Actually hit</th><th class="num">z</th></tr></thead><tbody>${rows.map((f) => `<tr>
      <td><b>${esc(f.name)}</b> <span class="rchip ${f.kind === "adjust" ? "adj" : "trk"}">${f.kind === "adjust" ? "adjusts" : "tracks"}</span></td>
      <td class="num">${f.n}</td><td class="num">${pct(f.crowd)}</td><td class="num">${pct(f.research)}</td>
      <td class="num">${pct(f.actual)} <span class="muted">(${f.hits}/${f.n})</span></td>
      <td class="num ${f.z == null ? "" : f.z >= 2 ? "pos" : f.z <= -2 ? "neg" : ""}">${f.z == null ? "-" : f.z}</td></tr>`).join("")}</tbody></table></div>` : "";
  const list = (cat || []).length ? `<details class="rs-notes"><summary>${ico("chevron", "ico ico-chev")} what the research layer checks, and why</summary>${cat.map((f) =>
    `<div class="rs-note"><b>${esc(f.name)}</b> <span class="rchip ${f.mode === "adjusts" ? "adj" : f.mode === "caution" ? "cau" : "trk"}">${mode[f.mode] || f.mode}</span><div>${esc(f.detail)}${f.source ? ` <a href="${esc(f.source)}" target="_blank" rel="noopener">source</a>` : ""}</div>${f.history ? `<div class="rs-hist">${esc(f.history)}</div>` : ""}</div>`).join("")}</details>` : "";
  const pool = football ? "every football board, pooled" : "every board with research, pooled";
  const note = football
    ? `A factor <b>adjusts</b> the Research % only where the market misprices it against the closing line, checked on every NFL game since 1999: today that is wind on NFL totals, plus rain from a published study. The rest are <b>tracked</b>: shown, graded, and left out of the number. The favorite bias failed that test (favorites won about as often as the de-vigged close said), so it moved to tracked.`
    : `Nothing adjusts the number on this board yet: every factor is <b>tracked</b>, shown on the game, graded, and left out of the number until it beats its price.`;
  return `<div class="pick-section"><h3>${ico("track")} Research study <span class="muted">· each factor's record against what the market priced (${pool})</span></h3>
    ${head}${tbl}
    <div class="cal-note muted"><span>${note} A z-score past +2 means a factor beat its price; near 0 means the market already prices it.</span></div>
    ${list}</div>`;
}

function renderLedger() {
  const box = $("#ledger-body"); if (!box) return;
  const ml = state.snapshot && state.snapshot.picks && state.snapshot.picks.model_ledger;
  if (!ml) { box.innerHTML = `<div class="muted" style="padding:14px">The Model Ledger fills in as the knockout games approach.</div>`; return; }
  const rows = ml.rows || [];
  const locked = rows.filter((r) => r.status === "locked").map((r) => ({
    a: r.team_a, b: r.team_b, model: [r.model_a, r.model_draw, r.model_b], legs: r.legs,
    market: [r.market_a, r.market_draw, r.market_b], kickoff_iso: r.kickoff_iso, frozen: true, lock_ts: r.lock_ts,
    research: _rowResearch(r), postponed: !!r.likely_postponed, time_tbd: !!r.time_tbd }));
  const upcoming = (ml.upcoming || []).map((c) => ({
    a: c.team_a, b: c.team_b, model: c.model, market: c.market, legs: c.legs, kickoff_iso: c.kickoff_iso, frozen: false,
    research: c.research, time_tbd: !!c.time_tbd }));
  const settled = rows.filter((r) => r.status === "settled");
  const hasModel = !!((state.snapshot || {}).meta || {}).model_loaded;
  // a game ESPN calls postponed or canceled waits after the live ones instead of heading the grid
  const cards = locked.filter((c) => !c.postponed).concat(upcoming, locked.filter((c) => c.postponed));
  const cardHtml = cards.length
    ? `<div class="pick-section"><h3>${ico("markets")} Forecasts <span class="muted">· locked freezes the line ${ml.buffer_min || 75} min before the start; preview is the live ${hasModel ? "model" : "market"} line until then</span></h3><div class="fgrid">${cards.map(_forecastCard).join("")}</div></div>`
    : "";
  let settledHtml = "";
  if (settled.length) {
    settledHtml = `<div class="pick-section"><h3>${ico("track")} Graded <span class="muted">· what the line said vs what happened, every line marked right or wrong${hasModel ? "; the 1X2 also scores the model against the market frozen at the same instant" : ""}</span></h3>
      ${_legAccuracy(settled, ml.leg_tally)}${_perfScore(settled, ml.perf_tally)}
      ${ml.settled_total > settled.length ? `<div class="muted" style="padding:0 2px 8px">The ${settled.length} most recent of ${ml.settled_total} graded games; the scores above count all of them.</div>` : ""}
      <div class="fgrid">${settled.map(_settledCard).join("")}</div></div>`;
  }
  const empty = (!cards.length && !settled.length)
    ? `<div class="muted" style="padding:14px">No forecasts yet. Each knockout game is logged here and freezes about ${ml.buffer_min || 75} minutes before kickoff.</div>` : "";
  // the research study lives on the Research tab now; the World Cup (no research) keeps nothing extra
  box.innerHTML = _ledgerScore(ml.summary || { n: 0, min_n: 8, ready: false }) + cardHtml + settledHtml + empty
    + _favPriceHTML(ml.fav_price);
}

function renderFutures() {
  const f = (state.snapshot && state.snapshot.picks && state.snapshot.picks.futures) || { rows: [], groups_covered: 0, sims: 0 };
  const box = $("#futures-body"); if (!box) return;
  const live = $("#scout-notes"); const noteVal = live ? live.value : scoutNotes();
  const notes = `<div class="scout"><label for="scout-notes">${ico("analyze")} Your scouting notes <span class="muted">· what you've seen watching the games, fed into every Read</span></label>
    <textarea id="scout-notes" rows="2" placeholder="e.g. Spain flat, created nothing vs Cabo Verde; France clinical; Mexico crowd is a real factor">${esc(noteVal)}</textarea></div>`;
  if (!f.rows.length) {
    box.innerHTML = notes + `<div class="empty">${ico("markets")}<div>No knockout futures yet. The bracket is rebuilt from finished group games; it fills in once all 12 groups are complete.${f.groups_covered ? ` <b>${f.groups_covered}/12</b> groups reconstructed so far.` : ""}</div></div>`;
    return;
  }
  if (f.archived) {
    const fin = f.final || {};
    const head = fin.winner ? `<div class="cal-note">${ico("check")}<span><b>Final:</b> ${esc(fin.winner_name || teamName(fin.winner))} ${fin.score_winner}-${fin.score_runner_up} ${esc(fin.runner_up_name || teamName(fin.runner_up))}${fin.date ? `, ${esc(_dateOnly(fin.date))}` : ""}. The bracket below is how it played out; the market and model odds on each tie are as they stood before it.</span></div>` : "";
    box.innerHTML = `<h2 class="ai-h">${ico("markets")} Knockout bracket <span class="muted">· World Cup 2026, final results</span></h2>` + head + renderBracket(f.bracket, true);
    return;
  }
  const byKind = {}, recs = f.records || {};
  f.rows.forEach((r) => { (byKind[r.kind] = byKind[r.kind] || []).push(r); });
  const row = (r) => `<tr><td><b>${esc(teamName(r.team))}</b></td>
      <td class="num"><b>${r.market_pct}%</b></td>
      <td class="num muted">${r.model_pct}%</td>
      <td class="num muted">${r.gap_pp > 0 ? "+" : ""}${r.gap_pp}pp</td>
      <td class="fut-act"><button class="act" data-fread data-team="${esc(r.team)}" data-kind="${esc(r.kind)}" data-mk="${r.market_pct}" data-md="${r.model_pct}" data-rec="${esc(recs[r.team] || "")}">Read</button><button class="act tiny" data-flean data-dir="back" data-team="${esc(r.team)}" data-kind="${esc(r.kind)}" data-pct="${r.market_pct}" title="Log a back: you think MORE likely than ${r.market_pct}%">Back</button><button class="act tiny" data-flean data-dir="fade" data-team="${esc(r.team)}" data-kind="${esc(r.kind)}" data-pct="${r.market_pct}" title="Log a fade: you think LESS likely than ${r.market_pct}%">Fade</button></td></tr>`;
  const section = (kind) => `<div class="pick-section"><h3>${esc(kind)}</h3>
    <table><thead><tr><th>Team</th><th class="num">Market</th><th class="num">Model</th><th class="num">Δ model</th><th class="num">Act</th></tr></thead>
    <tbody>${byKind[kind].map(row).join("")}</tbody></table></div>`;
  const locked = f.games_locked ? ` · ${f.games_locked} games locked` : "";
  const view = localStorage.getItem("overlay_futures_view") || "bracket";
  const toggle = `<span class="fview"><button class="act tiny ${view === "bracket" ? "on" : ""}" data-fview="bracket">Bracket</button><button class="act tiny ${view === "list" ? "on" : ""}" data-fview="list">List</button></span>`;
  const main = view === "list" ? Object.keys(byKind).map(section).join("") : (bracketKeyReads(f.bracket) + renderScenario() + renderBracket(f.bracket));
  box.innerHTML = `<h2 class="ai-h">${ico("markets")} Knockout futures <span class="muted">· de-vigged Polymarket vs model · ${f.sims.toLocaleString()} sims · ${f.groups_covered}/12 groups${locked}</span> ${toggle}</h2>`
    + `<div class="cal-note">${ico("shield")}<span><b>Market</b> is the de-vigged Polymarket price, the sharp vig-free probability and the number to trust. Each team's % is its odds to win that tie and advance; the small <b>tie</b> line gives market vs model for the top team. Switch to <b>List</b> to tap <b>Read</b> (an AI take weighing your notes) and log <b>Back</b> / <b>Fade</b> leans.</span></div>`
    + notes + renderLeans(f.leans) + main;
}

// one-line "why" for a tie, straight from the numbers: the favorite, how live the dog is, and where the
// model disagrees with the market (the spot the user's eye-test can add edge over the sharp line).
function matchupRead(mu) {
  if (mu.winner) return `${teamName(mu.winner)} won the tie.`;
  if (mu.mkt_a == null) return "";
  const aFav = mu.mkt_a >= 50;
  const fav = aFav ? mu.a : mu.b, dog = aFav ? mu.b : mu.a;
  const favMkt = aFav ? mu.mkt_a : 100 - mu.mkt_a;
  const favMdl = mu.mdl_a == null ? null : (aFav ? mu.mdl_a : 100 - mu.mdl_a);
  let s;
  if (favMkt >= 72) s = `${teamName(fav)} should win this; ${teamName(dog)} a longshot.`;
  else if (favMkt >= 58) s = `${teamName(fav)} favored, ${teamName(dog)} live (${100 - favMkt}%).`;
  else s = `Toss-up: ${teamName(fav)} ${favMkt}% vs ${teamName(dog)} ${100 - favMkt}%.`;
  if (favMdl != null) {
    const g = favMdl - favMkt;
    if (g <= -7 && favMdl <= 60) s += ` Model has it closer (${favMdl}%), so live value on ${teamName(dog)}.`;
    else if (g >= 7) s += ` Model rates ${teamName(fav)} even higher (${favMdl}%).`;
  }
  return s;
}

// the matchups that most deserve a decision: closest ties + biggest model-vs-market disagreements.
function bracketKeyReads(br) {
  if (!br || !br.rounds) return "";
  const all = [];
  br.rounds.forEach((r) => r.matchups.forEach((mu) => {
    if (mu.winner || mu.mkt_a == null) return;
    const closeness = Math.max(0, 22 - Math.abs(mu.mkt_a - 50));     // pick'em-ness
    const disagree = mu.mdl_a == null ? 0 : Math.abs(mu.mdl_a - mu.mkt_a);
    all.push({ mu, round: r.name, score: disagree * 2 + closeness });
  }));
  all.sort((a, b) => b.score - a.score);
  const top = all.filter((x) => x.score > 6).slice(0, 5);
  if (!top.length) return "";
  const items = top.map(({ mu, round }) =>
    `<li><b>${esc(teamName(mu.a))} v ${esc(teamName(mu.b))}</b> <span class="muted">${esc(round)}</span>: ${esc(matchupRead(mu))}</li>`).join("");
  return `<div class="cal-note keyreads">${ico("analyze")}<div><b>Where the decisions are:</b> the closest ties and where our model disagrees with the sharp line (that is where your eye-test can add edge):<ul class="kr">${items}</ul></div></div>`;
}

function _isPinned(mu, team) {
  return ((state.scenario && state.scenario.pins) || []).some((p) =>
    p.winner === team && ((p.a === mu.a && p.b === mu.b) || (p.a === mu.b && p.b === mu.a)));
}

// scenario explorer: pin knockout ties and watch the model's deep-run odds shift (it can't re-price the
// market, so this is the sim's conditional view).
function renderScenario() {
  const sc = state.scenario || {};
  if (!sc.pins || !sc.pins.length) return "";
  const chips = sc.pins.map((p) => {
    const loser = p.winner === p.a ? p.b : p.a;
    return `<span class="tag pin">${esc(teamName(p.winner))} over ${esc(teamName(loser))}<button class="pinx" data-pin-a="${esc(p.a)}" data-pin-b="${esc(p.b)}" data-pin-w="${esc(p.winner)}" title="remove">×</button></span>`;
  }).join(" ");
  const rows = (sc.deltas || []).map((d) => {
    const c = d.delta > 0 ? "pos" : d.delta < 0 ? "neg" : "muted";
    return `<tr><td><b>${esc(teamName(d.team))}</b></td><td class="muted">${esc(d.stage)}</td><td class="num">${d.base}% → ${d.scen}%</td><td class="num ${c}">${d.delta > 0 ? "+" : ""}${d.delta}pp</td></tr>`;
  }).join("");
  return `<div class="cal-note scen"><div style="flex:1"><b>Scenario:</b> ${chips} <button class="act tiny" data-scen-clear>clear</button>
    <div class="muted" style="margin:3px 0">how the model's deep-run odds shift if these play out (the market can't be re-priced for a hypothetical, so this is the sim's conditional view):</div>
    <table class="scen-tbl"><tbody>${rows || '<tr><td class="muted">no meaningful shifts</td></tr>'}</tbody></table></div></div>`;
}

function renderBracket(br, archived) {
  if (!br || !br.rounds || !br.rounds.length) return `<div class="muted" style="padding:14px">The bracket fills in as the knockout markets price up.</div>`;
  const teamCell = (team, pct, win, mu) => {
    if (!team) return `<div class="bteam tbd">TBD</div>`;
    const clickable = !archived && mu && !mu.winner && mu.a && mu.b;
    const pinned = mu && _isPinned(mu, team);
    const attrs = clickable ? ` data-pin-a="${esc(mu.a)}" data-pin-b="${esc(mu.b)}" data-pin-w="${esc(team)}"` : "";
    return `<div class="bteam ${win ? "bwin" : ""} ${pinned ? "bpin" : ""} ${clickable ? "bclick" : ""}"${attrs}><span class="bname">${esc(teamName(team))}</span><span class="bpct">${pct == null ? "" : pct + "%"}</span></div>`;
  };
  const match = (mu) => {
    const tie = mu.mkt_a == null ? "" : `<div class="btie">mkt ${mu.mkt_a}% · mdl ${mu.mdl_a == null ? "—" : mu.mdl_a + "%"}</div>`;
    return `<div class="bmatch ${mu.winner ? "done" : "pending"}" title="${esc(archived ? (mu.winner ? `${teamName(mu.winner)} went through` : "") : matchupRead(mu))}">${teamCell(mu.a, mu.a_pct, mu.winner === mu.a, mu)}${teamCell(mu.b, mu.b_pct, mu.winner === mu.b, mu)}${tie}</div>`;
  };
  const cols = br.rounds.map((r) => `<div class="bcol"><div class="bcol-h">${esc(r.name)}</div>${r.matchups.map(match).join("")}</div>`).join("");
  const champ = br.champion ? `<div class="bcol bcol-champ"><div class="bcol-h">Champion</div><div class="bmatch champ">${teamCell(br.champion, null, true)}</div></div>` : "";
  const hint = archived ? "" : `<div class="muted bhint">Tap a team in an undecided tie to pin them through and see the bracket shift.</div>`;
  return `<div class="bracket-wrap"><div class="bracket">${cols}${champ}</div></div>${hint}`;
}
function setPill(id, on) { $("#" + id).classList.toggle("live", !!on); }

// ---------- render: Best Bets (the hero, cards-first) ----------
function renderPicks() {
  const s = state.snapshot; if (!s) return;
  const p = s.picks || {}, m = s.meta;
  const ab = $("#ai-banner");
  const aiCount = Object.keys(p.ai || {}).length;
  if (m.archived) { ab.className = ""; ab.innerHTML = ""; $("#today-card").innerHTML = ""; renderArchive(p); return; }
  if (!m.ai_enabled) {
    ab.className = ""; ab.innerHTML = "";          // a keys-blank board: nothing to switch on here
  } else if (aiCount) {
    ab.className = "banner ok";
    ab.innerHTML = `${ico("check")}<span>AI analysis ready — <b>${aiCount}</b> match${aiCount > 1 ? "es" : ""} analyzed. Full reasoning is on the <b>Research</b> tab. Cached for today — re-running is free.</span>`;
  } else {
    ab.className = "banner warn";
    ab.innerHTML = `${ico("analyze")}<span><b>AI is on.</b> Tap <b>Analyze</b> (top-right) for reasoned verdicts on today's favorites — a few cents. Heuristic reads + favorites show below meanwhile.</span>`;
  }
  renderTodayCard(p);
  renderBestBets(p);
}

function renderTodayCard(p) {
  const box = $("#today-card");
  const sgp = (p.suggested_sgp && p.suggested_sgp.pricing) ? p.suggested_sgp : null;
  const parlay = p.parlay_of_day, pod = p.picks_of_day || [];
  if (!sgp && !parlay && !pod.length) { box.innerHTML = ""; return; }
  const tag = (a) => `<span class="tag">${esc((a || "").replace(/_/g, " "))}</span>`;
  let html = `<div class="card today"><h3>${ico("analyze")} Today's Card</h3><div class="today-grid">`;
  if (sgp) {
    const pr = sgp.pricing;
    const legs = sgp.legs.map((l) =>
      `<div class="leg2">${tag(l.type)} <b>${esc(l.selection)}</b>${l.prob != null ? ` <span class="muted">${Math.round(l.prob * 100)}%</span>` : ""}</div>`).join("");
    const evCls = pr.ev > 0 ? "pos" : "neg";
    html += `<div class="today-col">
      <div class="today-label">${ico("sgp")} Parlay of the Day <span class="muted">· same-game · ${esc(sgp.event)}</span></div>
      ${legs}
      <div class="sgp-pricing">
        <span><b>${Math.round(pr.joint_prob * 100)}%</b> to hit</span>
        <span>pays <b>${pr.payout}×</b></span>
        <span class="ev ${evCls}">${pr.ev > 0 ? "+" : ""}${pr.ev_pct}% EV</span>
        <span class="sgp-stake">${ico("odds")} ${sgp.stake_units}u · $${sgp.stake_dollars}</span>
      </div></div>`;
  } else if (parlay) {
    const legs = parlay.legs.map((l) =>
      `<div class="leg2">${tag(l.archetype)} <b>${esc(l.selection)}</b>${(!parlay.match && l.match) ? ` <span class="muted">${esc(l.match)}</span>` : ""}</div>`).join("");
    html += `<div class="today-col">
      <div class="today-label">${ico("sgp")} Parlay of the Day <span class="muted">· ${esc(parlay.type)}${parlay.match ? " · " + esc(parlay.match) : ""}</span></div>
      ${legs}
      ${parlay.confidence ? confMeter(parlay.confidence) : ""}</div>`;
  }
  if (pod.length) {
    html += `<div class="today-col"><div class="today-label">${ico("check")} Picks of the Day</div>`;
    pod.forEach((x) => {
      html += `<div class="pod-row"><span>${tag(x.archetype)} <b>${esc(x.selection)}</b><div class="muted">${esc(x.match)}${x.days_out != null ? " · " + dateLabel(x.days_out) : ""}</div></span><span class="num muted">${x.confidence}/5</span></div>`;
    });
    html += `</div>`;
  }
  box.innerHTML = html + `</div></div>`;
}

// the finished tournament: who won, and where its graded record lives
function renderArchive(p) {
  const fin = (p.futures || {}).final;
  const head = fin
    ? `<div class="card archive-card"><div class="today-label">${ico("check")} World Cup 2026 is over</div>
        <div class="arch-final"><b>${esc(fin.winner_name || teamName(fin.winner))}</b> ${fin.score_winner}-${fin.score_runner_up} <b>${esc(fin.runner_up_name || teamName(fin.runner_up))}</b> <span class="muted">· final, ${esc(_dateOnly(fin.date))}</span></div>`
    : `<div class="card archive-card"><div class="today-label">${ico("check")} World Cup 2026 is over</div>`;
  $("#bestbets").innerHTML = head + `<div class="muted">This board is the archive. Every game's graded line is on <b>Model Ledger</b>, the AI's picks and their record on <b>Track Record</b>, and the knockout bracket on <b>Futures</b>. The live boards are in the switcher on the left.</div></div>`;
}
const _dateOnly = (d) => { const x = new Date((d || "").slice(0, 10) + "T12:00:00"); return isNaN(x) ? (d || "") : x.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }); };

function renderBestBets(p) {
  const box = $("#bestbets"), bb = p.best_bets || [];
  const ai = ((state.snapshot || {}).meta || {}).ai_enabled;
  if (!bb.length) {
    box.innerHTML = `<div class="empty">${ico("picks")}<div>${ai ? "No bets surfaced yet. Tap Analyze for AI picks, or open the <b>Research</b> tab to browse candidates." : "No clear favorites on this board in the next few days."}</div></div>`;
    return;
  }
  const days = [...new Set(bb.map((c) => c.days_out).filter((d) => d != null))].sort((a, b) => a - b);
  const f = state.dayFilter;
  const chips = `<div class="day-filter"><button class="dchip ${f == null ? "on" : ""}" data-day="all">All days</button>`
    + days.map((d) => `<button class="dchip ${f === d ? "on" : ""}" data-day="${d}">${dateLabel(d)}</button>`).join("") + `</div>`;
  const shown = f == null ? bb : bb.filter((c) => c.days_out === f);
  const grid = shown.length
    ? `<div class="bet-grid">${shown.map(betCard).join("")}</div>`
    : `<div class="empty">no best bets for that day — try another, or Analyze</div>`;

  const fades = (p.fades || []).filter((x) => f == null || x.days_out === f);
  const fadeHtml = fades.length
    ? `<div class="fade-section"><h2 class="ai-h">${ico("fade")} Fade the crowd <span class="muted">· popular traps to avoid</span></h2>
       <div class="fade-grid">${fades.map(fadeCard).join("")}</div></div>`
    : "";

  // honesty: the prop board is a last-good fallback while PrizePicks throttles; say how old it really
  // is instead of letting the snapshot timestamp imply the lines are live
  const meta = (state.snapshot && state.snapshot.meta) || {};
  const ageS = meta.props_age_seconds;
  const propCards = bb.some((c) => c.archetype !== "favorite_ml" && c.archetype !== "total_corners");
  const staleBanner = (propCards && ageS != null && ageS > 4 * 3600)
    ? `<div class="cal-note">${ico("shield")}<span>Prop lines last fetched <b>${Math.round(ageS / 3600)}h ago</b> (PrizePicks is rate-limiting; serving the last good board). Check the live line before betting a prop.</span></div>`
    : "";

  // "best bets" only when a real price beats our number; otherwise these are the market's favorites
  const anyValue = bb.some((c) => c.value || (c.stake_units && c.source !== "model" && c.source !== "market"));
  const title = anyValue ? `Best bets <span class="muted">· a real price beats our number on these first</span>`
    : `Favorites <span class="muted">· the market's likeliest winners; no price beats our number right now, so there is no stake</span>`;
  box.innerHTML = `<h2 class="ai-h">${ico("picks")} ${title}</h2>${staleBanner}${chips}${grid}${fadeHtml}`;
}

const _VENUE = { kalshi: "Kalshi", polymarket: "Polymarket", draftkings: "DraftKings" };
// the card's team and game in the board's own names (Kalshi's short labels read "New York Y")
function _cardNames(c) {
  const f = ((state.snapshot || {}).picks || {}).favorite_ml || [];
  const hit = f.find((x) => `${x.team} ML` === c.selection && x.event === c.match);
  if (!hit || !hit.team_key) return { sel: c.selection, match: c.match };
  const parts = (c.match || "").split(" vs ");
  const name = (label) => (label === hit.team ? teamName(hit.team_key) : label === hit.opp ? teamName(hit.opp_key) : label);
  return { sel: `${teamName(hit.team_key)} ML`, match: parts.length === 2 ? `${name(parts[0])} vs ${name(parts[1])}` : c.match };
}
function betCard(c) {
  const srcLabel = { ai: "AI reasoned", read: "quick read", model: "model", market: "market" }[c.source] || c.source;
  const right = c.confidence ? confMeter(c.confidence) : (c.tier ? `<span class="tier tier-${c.tier}">${c.tier}</span>` : "");
  // model-vs-market disagreement chip (favorite ML cards carry both), same threshold as the Ledger
  const gap = (c.market_prob != null && c.model_prob != null)
    ? Math.round(Math.abs(c.market_prob - c.model_prob) * 100) : 0;
  const gapChip = gap >= 8 ? ` <span class="tag gap" title="model and market disagree by ${gap}pp on this favorite">Δ${gap}pp</span>` : "";
  const trap = c.trap ? '<span class="ob demon">trap</span>' : "";
  const venue = _VENUE[c.best_book] || c.best_book;
  const price = c.best_american != null
    ? `<div class="bc-price">${ico("best")}<span>best <b>${sgn(c.best_american)}</b> at ${esc(venue)}${c.ev_pct != null ? ` <span class="ev ${c.ev_pct > 0 ? "pos" : c.ev_pct < 0 ? "neg" : ""}">${c.ev_pct > 0 ? "+" : ""}${c.ev_pct}% ${c.research_prob != null ? "vs research" : "vs fair"}</span>` : ""}</span></div>`
    : "";
  // one value threshold per card: the research line carries it (with the cushion) when there is research
  const fair = c.market_prob != null && c.research_prob == null
    ? `<div class="bc-price">${ico("odds")}<span>fair <b>${sgn(fairAm(c.market_prob))}</b> <span class="muted">· value at <b>${_amer(_valueAt(c.market_prob, c.cushion || 0.02))}</b> or better</span></span></div>` : "";
  const model = c.model_prob != null
    ? `<div class="bc-model">${ico("track")} model <b>${Math.round(c.model_prob * 100)}%</b> to hit ${modelChip(c.model_prob, c.model_value)}</div>` : "";
  const cush = Math.round((c.cushion || 0.02) * 100);
  const rsLine = c.research_prob != null
    ? `<div class="bc-model" title="the market line plus the research; value needs a ${cush}% edge over it">${ico("track")}<span>research <b>${Math.round(c.research_prob * 100)}%</b> · value at <b>${_amer(c.value_at)}</b> or better</span></div>` : "";
  const dkLine = c.dk_price != null
    ? `<div class="bc-model">${ico("best")}<span>${esc(c.dk_book || "DraftKings")} <b>${_amer(c.dk_price)}</b> <span class="ev ${c.dk_ev == null ? "" : c.dk_ev * 100 >= cush ? "pos" : c.dk_ev < 0 ? "neg" : ""}">${_evTxt(c.dk_ev)} vs research</span></span></div>` : "";
  const rsChips = c.factors ? _rsChips(c.factors) : "";
  const stake = c.stake_units
    ? `<div class="bc-stake">${ico("odds")}<span>stake <b>${c.stake_units}u</b> <span class="muted">· $${c.stake_dollars}</span></span></div>` : "";
  const mem = c.memory_note ? `<div class="cal-note">${ico("track")} ${esc(c.memory_note)}</div>` : "";
  const research = c.research
    ? `<details class="ai-research"><summary>${ico("chevron", "ico ico-chev")} situational brief</summary><div class="body">${mdLite(c.research)}</div></details>` : "";
  const names = _cardNames(c);
  // the research chip already says "heavy favorite"; the chalk tag would say it twice
  const chalk = c.chalk && !(c.factors || []).some((f) => f.key === "fav_bias")
    ? ' <span class="tag chalk-tag" title="80% or more: safest legs, smallest payouts">heavy favorite</span>' : "";
  // a bare "80% sharp fair" repeats the research line below it
  const why = /sharp fair$/.test(c.reasoning || "") && (c.research_prob != null || c.market_prob != null) ? "" : c.reasoning;
  return `<div class="card bet-card">
    <div class="bc-top"><span class="tag">${esc((c.archetype || "").replace(/_/g, " "))}</span><span class="bc-src ${c.source}">${srcLabel}</span><span class="bc-r">${right}</span></div>
    <div class="bc-sel">${esc(names.sel)}${gapChip} ${trap}${chalk}</div>
    <div class="bc-meta">${esc(names.match || "")}${c.days_out != null ? " · " + dateLabel(c.days_out) : ""}${c.research_prob == null && c.market_prob != null ? ` · market <b>${Math.round(c.market_prob * 100)}%</b>` : ""}</div>
    ${why ? `<div class="bc-why">${mdLite(why)}</div>` : ""}
    ${fair}${price}${model}${rsLine}${dkLine}${rsChips}${stake}${mem}${research}
  </div>`;
}

function fadeCard(f) {
  return `<div class="card fade-card">
    <div class="fc-top">${ico("fade")}<b>${esc(f.selection)}</b></div>
    <div class="bc-meta">${esc(f.match || "")}${f.days_out != null ? " · " + dateLabel(f.days_out) : ""}</div>
    <div class="bc-why">${esc(f.reasoning || "")}</div>
  </div>`;
}

// ---------- render: Bet Slip (value now, the parlay checker, and the owner's own bets) ----------
const _amToDec = (a) => (a > 0 ? 1 + a / 100 : 1 + 100 / -a);
const _decToAm = (d) => (d >= 2 ? Math.round((d - 1) * 100) : Math.round(-100 / (d - 1)));
// the worst price that still clears the cushion at probability p (mirrors research.value_at)
const _valueAt = (p, c = 0.02) => { if (!p || p <= 0 || p >= 1) return null; const d = (1 + c) / p;
  return d >= 2 ? Math.ceil(+((d - 1) * 100).toFixed(6)) : -Math.floor(+(100 / (d - 1)).toFixed(6)); };
const _slipKey = () => "overlay_slip_" + (((state.snapshot || {}).meta || {}).sport || "x");
function _slipLoad() {
  if (state.slip && state.slip.sport === ((state.snapshot || {}).meta || {}).sport) return state.slip;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(_slipKey()) || "null"); } catch (e) { saved = null; }
  state.slip = { sport: ((state.snapshot || {}).meta || {}).sport, legs: [], price: "", stake: "", book: "", ...(saved || {}) };
  return state.slip;
}
function _slipSave() { try { localStorage.setItem(_slipKey(), JSON.stringify(state.slip)); } catch (e) { /* private mode: the slip still works for this visit */ } }
const _legLabel = (l) => l.label ? l.label : l.kind === "ml" ? `${teamName(l.team)} ML`
  : l.kind === "spread" ? `${teamName(l.team)} ${l.line > 0 ? "+" : ""}${l.line}`
  : `${l.dir === "over" ? "Over" : "Under"} ${l.line}`;
const _legId = (l) => [l.dedup, l.kind, l.team || l.dir, l.line].join("|");
// every pickable side of one live game, with its current Research % (the market's where nothing moved it)
function _gameSides(g) {
  const out = [g.team_a, g.team_b].map((t) => ({ dedup: g.dedup, kind: "ml", team: t, p: (g.ml || {})[t] }));
  if (g.spread) {
    const other = g.spread.team === g.team_a ? g.team_b : g.team_a;
    out.push({ dedup: g.dedup, kind: "spread", team: g.spread.team, line: -g.spread.line, p: g.spread.p });
    out.push({ dedup: g.dedup, kind: "spread", team: other, line: g.spread.line, p: 1 - g.spread.p });
  }
  if (g.total) {
    out.push({ dedup: g.dedup, kind: "total", dir: "over", line: g.total.line, p: g.total.p_over });
    out.push({ dedup: g.dedup, kind: "total", dir: "under", line: g.total.line, p: 1 - g.total.p_over });
  }
  // each pair's labels add to 100 (rounding both halves on their own can read 49 / 52)
  for (let i = 0; i + 1 < out.length; i += 2) {
    if (out[i].p == null || out[i + 1].p == null) continue;
    out[i].pct = Math.round(out[i].p * 100); out[i + 1].pct = 100 - out[i].pct;
  }
  return out;
}
function _livePrice(leg) {   // the leg's Research % right now, from the live board
  const g = (((state.snapshot || {}).picks || {}).slip_games || []).find((x) => x.dedup === leg.dedup);
  if (!g) return leg.p;
  const hit = _gameSides(g).find((x) => _legId(x) === _legId(leg));
  return hit ? hit.p : leg.p;
}
function _valueNowHTML(rows) {
  const research = !!(((state.snapshot || {}).picks || {}).model_ledger || {}).research_catalog;
  const ours = research ? "the Research %" : "the market's fair price";
  if (!rows || !rows.length) return `<div class="pick-section"><h3>${ico("value")} Value now <span class="muted">· lines where a real price beats ${ours} by its cushion</span></h3><div class="muted" style="padding:6px 2px">Nothing clears the cushion right now. Prices move as games approach; this list refreshes with the board.</div></div>`;
  const row = (r) => {
    const leg = { dedup: r.dedup, kind: r.kind, team: r.team, dir: r.dir, line: r.line, p: r.p };
    return `<div class="vn-row"><span><b>${esc(_legLabel(leg))}</b> <span class="muted">${esc(teamName(r.team_a))} v ${esc(teamName(r.team_b))} · ${_koLabel(r.kickoff_iso, r.time_tbd)}</span></span>
      <span class="vn-r"><span>${esc(r.venue)} <b>${_amer(r.price)}</b> <span class="ev pos">${_evTxt(r.ev)}</span></span>
      <button class="act tiny" data-slip-add='${esc(JSON.stringify({ ...leg, price: r.price, venue: r.venue }))}'>add to slip</button></span></div>`;
  };
  return `<div class="pick-section"><h3>${ico("value")} Value now <span class="muted">· ${rows.length} line${rows.length > 1 ? "s" : ""} where a real price beats ${ours} by its cushion (2%, more while a question is open)</span></h3>${rows.map(row).join("")}</div>`;
}
function _parlayHTML(slip, games) {
  const legs = slip.legs.map((l) => ({ ...l, p: _livePrice(l) }));
  const P = legs.length && legs.every((l) => l.p) ? legs.reduce((a, l) => a * l.p, 1) : null;
  const price = parseInt(slip.price, 10);
  const priceOk = !isNaN(price) && Math.abs(price) >= 100;
  const ev = P && priceOk ? P * _amToDec(price) - 1 : null;
  const sameGame = new Set(legs.map((l) => l.dedup)).size < legs.length;
  const verdict = ev == null ? "" : ev >= 0.02 ? `<span class="ev pos">${_evTxt(ev)} expected: worth it</span>`
    : ev >= 0 ? `<span class="ev">${_evTxt(ev)} expected: too thin</span>` : `<span class="ev neg">${_evTxt(ev)} expected: not worth it</span>`;
  const legRows = legs.length ? legs.map((l) => `<div class="vn-row"><span>${l.sport && l.sport !== ((state.snapshot || {}).meta || {}).sport ? `<span class="tb-plat p">${esc(l.sport.toUpperCase())}</span> ` : ""}<b>${esc(_legLabel(l))}</b> <span class="muted">${l.game ? esc(l.game) : esc(teamName(l.team_a || "")) + (l.team_a ? " v " + esc(teamName(l.team_b)) : "")}</span></span><span class="vn-r"><span class="muted">${l.p ? (l.p * 100).toFixed(1) + "%" : "-"}</span><button class="act tiny" data-slip-rm="${esc(_legId(l))}">×</button></span></div>`).join("")
    : `<div class="muted" style="padding:6px 2px">Add legs from Value now or from the games below.</div>`;
  const stats = P ? `<div class="summary lg-score slip-stats">
      <div class="stat"><div class="label">Chance all hit</div><div class="val">${(P * 100).toFixed(1)}%</div></div>
      <div class="stat"><div class="label">Fair odds</div><div class="val">${_amer(_decToAm(1 / P))}</div></div>
      <div class="stat"><div class="label">Worth it at</div><div class="val">${_amer(_valueAt(P))} <span class="muted" style="font-size:12px">or better</span></div></div>
    </div>` : "";
  const warn = sameGame ? `<div class="cal-note">${ico("shield")} <span>Same-game legs move together, and books price that in. This math treats them as independent, so read it as rough.</span></div>` : "";
  const form = legs.length ? `<div class="slip-form">
      <label>Your book's odds <input id="slip-price" inputmode="numeric" placeholder="+450" value="${esc(slip.price)}"></label>
      <label>Stake <input id="slip-stake" inputmode="decimal" placeholder="10" value="${esc(slip.stake)}"></label>
      <label>Book <input id="slip-book" placeholder="FanDuel" value="${esc(slip.book)}" maxlength="30"></label>
      <span class="slip-verdict">${verdict}</span>
      <button class="btn" id="slip-log" ${priceOk && parseFloat(slip.stake) > 0 ? "" : "disabled"}>Log this bet</button>
      <button class="act tiny" id="slip-clear">clear</button></div>` : "";
  const filt = (state.slipFilter || "").toLowerCase();
  const shown = (games || []).filter((g) => !filt || (teamName(g.team_a) + " " + teamName(g.team_b)).toLowerCase().includes(filt));
  const inSlip = new Set(slip.legs.map(_legId));
  const picker = `<div class="slip-pick"><input id="slip-filter" placeholder="find a team" value="${esc(state.slipFilter || "")}">
    ${shown.slice(0, 40).map((g) => `<div class="sp-game"><div class="sp-h"><b>${esc(teamName(g.team_a))}</b> <span class="muted">v</span> <b>${esc(teamName(g.team_b))}</b> <span class="muted">${_koLabel(g.kickoff_iso, g.time_tbd)}</span></div>
      <div class="sp-sides">${_gameSides(g).map((x) => `<button class="sp-side ${inSlip.has(_legId(x)) ? "on" : ""}" data-slip-add='${esc(JSON.stringify(x))}'>${esc(_legLabel(x))} <span class="muted">${x.pct != null ? x.pct + "%" : x.p ? Math.round(x.p * 100) + "%" : ""}</span></button>`).join("")}</div></div>`).join("")}
    ${shown.length > 40 ? `<div class="muted">${shown.length - 40} more: narrow it with the search</div>` : ""}</div>`;
  return `<div class="pick-section"><h3>${ico("sgp")} Parlay checker <span class="muted">· the true chance it hits, from the Research % of each leg, against your book's payout</span></h3>${legRows}${stats}${warn}${form}
    <details class="rs-notes" ${slip.legs.length ? "" : "open"}><summary>${ico("chevron", "ico ico-chev")} add legs from any game</summary>${picker}</details></div>`;
}
function _myBetsHTML(d) {
  if (!d) return `<div class="pick-section"><h3>${ico("bets")} My bets</h3><div class="muted" style="padding:6px 2px">Loading…</div></div>`;
  const s = d.summary || {};
  const stat = (label, v) => `<div class="stat"><div class="label">${label}</div><div class="val">${v}</div></div>`;
  const head = s.bets ? `<div class="summary lg-score">
      ${stat("Record", `${s.won}-${s.lost}${s.push ? "-" + s.push : ""}`)}
      ${stat("Profit", `<span class="${s.profit > 0 ? "pos" : s.profit < 0 ? "neg" : ""}">${s.profit >= 0 ? "+" : "-"}$${Math.abs(s.profit).toFixed(2)}</span>${s.roi != null ? ` <span class="muted" style="font-size:13px">${s.roi > 0 ? "+" : ""}${s.roi}% ROI</span>` : ""}`)}
      ${stat("Avg CLV", s.clv_avg == null ? "-" : `<span class="${s.clv_avg > 0 ? "pos" : s.clv_avg < 0 ? "neg" : ""}">${s.clv_avg > 0 ? "+" : ""}${s.clv_avg}%</span>`)}
      ${stat("Beat the close", s.beat_close == null ? "-" : s.beat_close + "%")}
    </div><div class="cal-note muted"><span><b>CLV</b> is what your price was worth at the closing line: positive means you got a better number than the market settled on. Over many bets it predicts profit far better than the win-loss record does.</span></div>` : "";
  const rows = (d.bets || []).map((b) => `<tr>
      <td>${b.legs.map((l) => `<div><b>${esc(_legLabel(l))}</b> <span class="muted">${esc(teamName(l.team_a))} v ${esc(teamName(l.team_b))}</span></div>`).join("")}</td>
      <td>${esc(b.book || "")}</td><td class="num">${_amer(b.price)}</td><td class="num">$${b.stake}</td>
      <td><span class="tag ${b.status === "won" ? "won" : b.status === "lost" ? "lost" : ""}">${esc(b.status)}</span></td>
      <td class="num ${b.profit > 0 ? "pos" : b.profit < 0 ? "neg" : ""}">${b.profit == null ? "" : (b.profit >= 0 ? "+" : "-") + "$" + Math.abs(b.profit).toFixed(2)}</td>
      <td class="num ${b.clv > 0 ? "pos" : b.clv < 0 ? "neg" : ""}" title="value at the close">${b.clv == null ? "-" : _evTxt(b.clv)}</td>
      <td><button class="act tiny" data-bet-rm="${b.id}" title="delete this bet">×</button></td></tr>`).join("");
  const table = rows ? `<div class="tbl-scroll"><table class="flat"><thead><tr><th>Bet</th><th>Book</th><th class="num">Price</th><th class="num">Stake</th><th>Status</th><th class="num">Profit</th><th class="num">CLV</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
    : `<div class="muted" style="padding:6px 2px">No bets logged yet. Build one above and tap <b>Log this bet</b>.</div>`;
  return `<div class="pick-section"><h3>${ico("bets")} My bets <span class="muted">· what you actually bet, graded off the final and scored against the close</span></h3>${head}${table}</div>`;
}
function renderSlip(force) {
  const box = $("#slip-body"); if (!box) return;
  // the board refreshes on a timer; never yank a field out from under someone typing in it
  if (!force && box.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  const p = (state.snapshot && state.snapshot.picks) || {};
  const slip = _slipLoad();
  box.innerHTML = _valueNowHTML(p.value_now) + _parlayHTML(slip, p.slip_games) + _myBetsHTML(state.mybets);
}
async function loadMyBets() {
  try { state.mybets = await getJSON("api/mybets"); } catch (e) { state.mybets = { bets: [], summary: {} }; }
  if (state.tab === "slip") renderSlip(true);
}
document.addEventListener("click", async (e) => {
  const add = e.target.closest("[data-slip-add]");
  if (add) {
    const leg = JSON.parse(add.getAttribute("data-slip-add"));
    const slip = _slipLoad();
    const g = (((state.snapshot || {}).picks || {}).slip_games || []).find((x) => x.dedup === leg.dedup) || {};
    const id = _legId(leg);
    if (slip.legs.some((l) => _legId(l) === id)) slip.legs = slip.legs.filter((l) => _legId(l) !== id);
    else {
      slip.legs = slip.legs.filter((l) => !(l.dedup === leg.dedup && l.kind === leg.kind));   // one side per line
      slip.legs.push({ ...leg, team_a: g.team_a, team_b: g.team_b });
      if (leg.price && slip.legs.length === 1) { slip.price = String(leg.price); slip.book = leg.venue || slip.book; }
    }
    _slipSave(); renderSlip(true); return;
  }
  const rm = e.target.closest("[data-slip-rm]");
  if (rm) { const slip = _slipLoad(); slip.legs = slip.legs.filter((l) => _legId(l) !== rm.getAttribute("data-slip-rm")); _slipSave(); renderSlip(true); return; }
  if (e.target.id === "slip-clear") { const slip = _slipLoad(); slip.legs = []; slip.price = ""; _slipSave(); renderSlip(true); return; }
  if (e.target.id === "slip-log") {
    const slip = _slipLoad();
    const body = { legs: slip.legs.map((l) => ({ dedup: l.dedup, kind: l.kind, team: l.team, dir: l.dir, line: l.line })),
                   price: parseInt(slip.price, 10), stake: parseFloat(slip.stake), book: slip.book };
    try {
      await getJSON("api/mybets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      slip.legs = []; slip.price = ""; _slipSave();
      await loadMyBets(); renderSlip(true);
    } catch (err) {
      let msg = err.message; try { msg = JSON.parse(msg).detail || msg; } catch (x) { /* plain text */ }
      alert("Could not log the bet: " + msg);
    }
    return;
  }
  const del = e.target.closest("[data-bet-rm]");
  if (del && confirm("Delete this bet from your record?")) {
    await getJSON("api/mybets/" + del.getAttribute("data-bet-rm"), { method: "DELETE" });
    await loadMyBets();
  }
});
document.addEventListener("input", (e) => {
  const map = { "slip-price": "price", "slip-stake": "stake", "slip-book": "book" };
  if (map[e.target.id]) {
    const slip = _slipLoad(); slip[map[e.target.id]] = e.target.value; _slipSave();
    const pos = e.target.selectionStart, id = e.target.id;
    renderSlip(true);
    const el = document.getElementById(id); if (el) { el.focus(); try { el.setSelectionRange(pos, pos); } catch (x) { /* number inputs */ } }
  } else if (e.target.id === "slip-filter") {
    state.slipFilter = e.target.value;
    const pos = e.target.selectionStart;
    renderSlip(true);
    const el = document.getElementById("slip-filter"); if (el) { el.focus(); el.setSelectionRange(pos, pos); }
  }
});

// ---------- render: Lotto ($1-$5 weekend parlays built to pay $1,000+ at the book, every weekend's tickets tracked) ----------
const _LOTTO_FLAGS = { heavy: ["heavy favorite", "trk"], value: ["priced above our number", "adj"], top: ["top bettors' side", "trk"],
                       research: ["research: wind", "adj"], no_report: ["no injury report", "cau"], dog: ["moderate underdog", "trk"] };
const _LOTTO_VARIANTS = [["efficient", "least lost to the book"], ["favorites", "most likely favorites"], ["research", "research legs first"]];
const _variantName = (v) => (_LOTTO_VARIANTS.find(([k]) => k === v) || [v, v])[1];
const _money0 = (n) => "$" + Math.round(n).toLocaleString();
function _lottoState() {
  if (!state.lotto) {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("overlay_lotto4") || "null"); } catch (e) { saved = null; }
    state.lotto = { stake: 5, target: 1000, variant: "efficient", ...(saved || {}) };
    if (!_LOTTO_VARIANTS.some(([k]) => k === state.lotto.variant)) state.lotto.variant = "efficient";
  }
  return state.lotto;
}
function _lottoSave() { try { localStorage.setItem("overlay_lotto4", JSON.stringify(state.lotto)); } catch (e) { /* this visit only */ } }
const _flagChips = (flags, edge) => (flags || []).filter((f) => f !== "heavy")
  .map((f) => { const val = f === "value" && edge;
    return `<span class="rchip ${(_LOTTO_FLAGS[f] || [])[1] || ""}" ${val ? `title="${esc(`${edge.venue} ${_amer(edge.price)} is ${_evTxt(edge.ev)} over our number`)}"` : ""}>${esc((_LOTTO_FLAGS[f] || [f])[0])}${val ? ` ${esc(edge.venue)} ${_amer(edge.price)}` : ""}</span>`; }).join(" ");
function _lottoLegRow(l) {
  const res = l.result ? `<span class="tag ${l.result === "won" ? "won" : l.result === "lost" ? "lost" : ""}">${esc(l.result)}</span>` : "";
  return `<div class="lt-leg"><span><span class="tb-plat p">${esc((l.sport || "").toUpperCase())}</span> <b>${esc(l.label)}</b> <span class="muted">${esc(l.game)} · ${_koLabel(l.kickoff_iso)}</span> ${_flagChips(l.flags, l.edge)}</span>
    <span class="lt-p">${res} ${l.dk ? `<span class="muted">DK ${_amer(l.dk)}</span> · ` : ""}${Math.round(l.p * 100)}%</span></div>`;
}
function _lottoTicketHTML(t) {
  const P = t.p, stake = t.stake, n = t.legs.length;
  const exact = n > 0 && t.dk_priced === n;
  const pay = t.dk_payout || t.fair_payout;
  const cut = t.dk_payout && t.fair_payout ? Math.round((1 - t.dk_payout / t.fair_payout) * 100) : null;
  const miss = t.reached ? "" : t.variant === "efficient"
    ? `<div class="cal-note">${ico("shield")} <span>No 20 DraftKings-priced legs reach ${_money0(t.target)} yet (games get a DraftKings price inside five days of kickoff). This is the biggest one there is; "most likely favorites" can reach it with estimated prices.</span></div>`
    : `<div class="cal-note">${ico("shield")} <span>No 20 sensible legs pay ${_money0(t.target)} at the book this weekend yet. This is the biggest one there is; more games fill in as the week goes on.</span></div>`;
  const est = exact ? "" : ` <span class="muted">(${n - (t.dk_priced || 0)} leg${n - (t.dk_priced || 0) === 1 ? "" : "s"} without a DraftKings price yet, estimated at this weekend's typical margin for the price)</span>`;
  const book = cut != null ? `<div class="rs-line">Fair odds would pay ${_money0(t.fair_payout)}; the book keeps about <b>${cut}%</b> of this ticket's value${est}.</div>` : "";
  const how = t.variant === "efficient" ? `<div class="cal-note muted"><span>Legs picked to lose the least to the book for each dollar of payout: mid-priced favorites, ${t.dogs ? `${t.dogs} moderate underdog${t.dogs === 1 ? "" : "s"} (35% to 50%)` : "and up to two moderate underdogs when DraftKings prices them near our number"}, and any leg priced above our number first. Heavy favorites cost the most per dollar of payout, so they only top a ticket off.</span></div>` : "";
  return `<div class="fcard lt-card">
    <div class="lt-hero"><span class="lt-odds">${exact ? "" : "~"}${esc(_money0(pay))}</span><span class="muted">${t.dk_payout ? "at DraftKings" : "at fair odds"} on $${stake} · 1 in ${Math.round(1 / P).toLocaleString()} to hit · ${n} legs${t.dk_price ? ` · ${_amer(t.dk_price)}` : ""}</span></div>
    ${book}${miss}${how}
    <div class="lt-legs">${t.legs.map(_lottoLegRow).join("")}</div>
    <button class="btn lt-load" data-lotto-load="1">Load into Bet Slip</button>
  </div>`;
}
// a weekend is keyed by the Tuesday it ends on; people know it by its Saturday to Monday
const _weekendLabel = (w) => { const end = new Date(w + "T12:00:00Z"); if (isNaN(end)) return "Weekend " + w;
  const sat = new Date(end); sat.setUTCDate(end.getUTCDate() - 3); const mon = new Date(end); mon.setUTCDate(end.getUTCDate() - 1);
  const f = (d) => d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
  return `Weekend of ${f(sat)} to ${f(mon)}`; };
function _lottoTrackedHTML(lotto) {
  const tracked = lotto.tracked || [];
  const weekend = lotto.weekend;
  const now = tracked.filter((t) => t.weekend === weekend);
  const fz = new Date(lotto.freeze_at);
  const waiting = !now.length ? `<div class="muted" style="padding:6px 2px">This weekend's nine $5 tickets ($1,000, $2,500 and $5,000, each built three ways: least lost to the book, most likely favorites, research first) freeze ${isNaN(fz) ? "Saturday morning" : fz.toLocaleString(undefined, { weekday: "long", hour: "numeric", minute: "2-digit" })} and grade on their own, bet or not.</div>` : "";
  const row = (t) => {
    const status = t.status === "hit" ? `<span class="tag won">hit</span>` : t.status === "missed" ? `<span class="tag lost">missed</span>` : `<span class="tag">live</span>`;
    const legs = `${t.legs_won}/${t.legs.length - (t.legs_void || 0)} legs won${t.legs_void ? `, ${t.legs_void} void` : ""}`;
    return `<details class="tb-line"><summary><span class="tb-side">${esc(_money0(t.target))}+ · ${esc(_variantName(t.variant))} ${status}</span><span class="tb-sum">${legs} · pays ${esc(_money0(t.dk_payout || t.fair_payout))}${t.dk_payout ? " at DK" : ""} · 1 in ${Math.round(1 / t.p).toLocaleString()}</span></summary>${t.legs.map(_lottoLegRow).join("")}</details>`;
  };
  const byWeekend = {};
  tracked.forEach((t) => (byWeekend[t.weekend] = byWeekend[t.weekend] || []).push(t));
  const blocks = Object.keys(byWeekend).sort().reverse().map((w) => `<div class="lt-week"><div class="muted lt-wk">${esc(_weekendLabel(w))}</div>${byWeekend[w].map(row).join("")}</div>`).join("");
  return `<div class="pick-section"><h3>${ico("track")} Tracked tickets <span class="muted">· frozen every Saturday morning and graded leg by leg, whether you bet them or not</span></h3>${waiting}${blocks}</div>`;
}
function _lottoStudyHTML(st) {
  if (!st || !st.legs || !st.legs.length) return `<div class="pick-section"><h3>${ico("value")} What the tickets are teaching</h3><div class="muted" style="padding:6px 2px">Fills in as the first tracked legs grade. Each row compares how often a kind of leg actually won with what its price said; a group that keeps winning less than priced comes off future tickets.</div></div>`;
  const pct = (v) => (v != null ? v.toFixed(1) + "%" : "-");
  const names = { all: "All legs", style: "Construction", sport: "Sport", band: "Price", flag: "Research flag" };
  const rows = st.legs.map((r) => `<tr><td><span class="muted">${esc(names[r.group] || r.group)}</span> <b>${esc(r.group === "style" ? _variantName(r.key.split(":")[1]) : r.label)}</b></td><td class="num">${r.n}</td><td class="num">${pct(r.priced)}</td><td class="num">${pct(r.actual)}</td><td class="num ${r.z == null ? "" : r.z >= 2 ? "pos" : r.z <= -2 ? "neg" : ""}">${r.z == null ? "-" : r.z}</td></tr>`).join("");
  const close = (st.closest || []).length ? `<div class="cal-note muted"><span>Closest calls: ${st.closest.map((c) => `${c.won} of ${c.legs} legs (${esc(_money0(c.target))}+ ${esc(_variantName(c.variant))}, ${esc(c.weekend)})`).join(" · ")}</span></div>` : "";
  return `<div class="pick-section"><h3>${ico("value")} What the tickets are teaching <span class="muted">· ${st.graded} graded of ${st.tickets} tickets over ${st.weekends} weekend${st.weekends === 1 ? "" : "s"}; ${st.hits} hit against ${st.expected_hits} expected</span></h3>
    <div class="tbl-scroll"><table class="flat"><thead><tr><th>Legs</th><th class="num">Graded</th><th class="num">Priced to win</th><th class="num">Actually won</th><th class="num">z</th></tr></thead><tbody>${rows}</tbody></table></div>${close}</div>`;
}
function renderLotto() {
  const box = $("#lotto-body"); if (!box) return;
  const lotto = (state.snapshot && state.snapshot.picks && state.snapshot.picks.lotto) || null;
  if (!lotto) { box.innerHTML = `<div class="muted" style="padding:14px">The lotto builder fills in once the boards have games this weekend.</div>`; return; }
  const ls = _lottoState();
  const found = (lotto.tickets || []).find((x) => x.stake === ls.stake && x.target === ls.target && x.variant === ls.variant);
  // tickets name their legs by position in the pool; older snapshots carried the legs themselves
  const t = found && (found.legs ? found : { ...found, legs: (found.leg_ids || []).map((i) => (lotto.legs || [])[i]).filter(Boolean) });
  state.lottoTicket = t;
  // the boards that fed this weekend's legs (a board with no games, like the World Cup archive, is not one)
  const boards = [...new Set((lotto.legs || []).map((l) => l.sport))].sort();
  const controls = `<div class="slip-form lt-controls">
      <label>Stake <select id="lotto-stake">${[1, 2, 3, 4, 5].map((k) => `<option value="${k}" ${k === ls.stake ? "selected" : ""}>$${k}</option>`).join("")}</select></label>
      <label>Pays at least <select id="lotto-target">${[1000, 2500, 5000].map((k) => `<option value="${k}" ${k === ls.target ? "selected" : ""}>${_money0(k)}</option>`).join("")}</select></label>
      <label>Legs <select id="lotto-variant">${_LOTTO_VARIANTS.map(([k, v]) => `<option value="${k}" ${k === ls.variant ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    </div>`;
  const note = `<details class="lt-how"><summary>${ico("chevron", "ico ico-chev")} <b>The payout sets the odds, and the book takes a cut of every leg</b></summary><div class="cal-note muted"><span>At fair prices $5 to win $1,000 is about 1 in 200 whatever the legs. A sportsbook pays a little under fair on each leg and the cut compounds (US books keep 20 to 30% of parlay money), so the same $1,000 at DraftKings is more like 1 in 250 to 1 in 400. How much depends on the legs: heavy favorites lose the most per dollar of payout, since it takes so many of them, while legs from about 35% to 75% lose the least. That is why "least lost to the book" mixes in up to two moderate underdogs, never long shots. The other levers: a bigger stake (up to $5 here), and a parlay profit boost, which on a ticket this long can cancel most of the book's cut. <b>${(lotto.legs || []).length}</b> sensible legs this weekend across ${boards.map((b) => b.toUpperCase()).join(", ") || "no boards"}.</span></div></details>`;
  box.innerHTML = controls + note + (t ? _lottoTicketHTML(t) : `<div class="muted" style="padding:14px">No sensible legs through this weekend yet.</div>`)
    + _lottoTrackedHTML(lotto) + _lottoStudyHTML(lotto.study);
}
document.addEventListener("change", (e) => {
  const ls = state.lotto; if (!ls) return;
  const map = { "lotto-stake": "stake", "lotto-target": "target", "lotto-variant": "variant" };
  if (!map[e.target.id]) return;
  ls[map[e.target.id]] = e.target.id === "lotto-variant" ? e.target.value : parseInt(e.target.value, 10);
  _lottoSave(); renderLotto();
});
document.addEventListener("click", (e) => {
  if (!e.target.closest("[data-lotto-load]")) return;
  const t = state.lottoTicket;
  if (!t) return;
  const slip = _slipLoad();
  slip.legs = t.legs.map((l) => ({ dedup: l.dedup, kind: l.kind, team: l.team, dir: l.dir, line: l.line, p: l.p,
                                   team_a: l.team_a, team_b: l.team_b, label: l.label, game: l.game, sport: l.sport }));
  slip.stake = String(t.stake);
  slip.price = t.dk_price ? String(t.dk_price) : "";             // DraftKings' price for the whole ticket, when known
  _slipSave();
  switchTab("slip");
  renderSlip(true);
});

// ---------- render: Top Bettors (what the leaderboards' best sports traders hold on this board) ----------
const _money = (n) => (n == null ? "-" : n >= 1e6 ? "$" + (n / 1e6).toFixed(1) + "M" : n >= 1000 ? "$" + (n / 1000).toFixed(1) + "k" : "$" + Math.round(n));
const _cents = (p) => (p == null ? "" : Math.round(p * 100) + "¢");
// some wallets have no username, just an address-like id: keep the ends so it stays recognizable
const _handle = (n) => { n = String(n || ""); return n.length > 22 ? n.slice(0, 10) + "…" + n.slice(-6) : n; };
function _topLine(l) {
  if (l.kind === "ml") return `${teamName(l.team)} ML`;
  if (l.kind === "spread") return `${teamName(l.team)} ${l.line > 0 ? "+" : ""}${l.line}`;
  if (l.kind === "total") return `${l.dir === "over" ? "Over" : "Under"} ${l.line}`;
  return l.kind;
}
function _topWho(r) {
  const tags = (r.two_sided ? ` <span class="rchip cau" title="holds both sides of this market; shown net">both sides</span>` : "")
    + (r.mm ? ` <span class="rchip cau" title="huge volume at a thin profit margin: likely a market maker, so this is inventory, not a pick">market maker?</span>` : "");
  const size = r.contracts != null ? `${r.contracts.toLocaleString()} contracts (~${_money(r.stake)})` : _money(r.stake);
  return `<div class="tb-row"><span class="tb-who"><span class="tb-plat ${r.platform === "Kalshi" ? "k" : "p"}">${r.platform === "Kalshi" ? "K" : "PM"}</span> <b title="${esc(r.name)}">${esc(_handle(r.name))}</b> <span class="muted">#${r.rank} ${esc(r.window)} · ${_money(r.pnl)} profit</span>${tags}</span><span class="tb-amt">${size}${r.price != null ? ` <span class="muted">@ ${_cents(r.price)}</span>` : ""}</span></div>`;
}
function _topGame(g) {
  const sp = g.market ? _split(g.market) : null;
  const mk = sp ? `<div class="rs-line muted">market ${esc(teamName(g.team_a))} ${sp[0]}% · ${esc(teamName(g.team_b))} ${sp[2]}%</div>` : "";
  const c = g.consensus;
  const cons = c ? `<div class="rs-line" title="directional traders only: market makers and wallets on both sides are left out">top-bettor money on the moneyline: <b>${esc(teamName(c.team))}</b> ${Math.round(c.share * 100)}% of ${_money(c.total)} <span class="muted">(${c.traders} directional trader${c.traders > 1 ? "s" : ""}; market makers left out)</span></div>` : "";
  const lines = g.lines.map((l) => `<details class="tb-line"><summary><span class="tb-side">${esc(_topLine(l))}</span><span class="tb-sum">${l.traders} trader${l.traders > 1 ? "s" : ""} · <b>${_money(l.stake)}</b></span></summary>${l.rows.map(_topWho).join("")}</details>`).join("");
  return `<div class="fcard">
    <div class="fcard-h"><span class="fcard-m"><b>${esc(teamName(g.team_a))}</b> <span class="muted">v</span> <b>${esc(teamName(g.team_b))}</b></span><span class="fcard-k muted">${_koLabel(g.kickoff_iso)}</span></div>
    ${mk}${cons}<div class="tb-lines">${lines}</div></div>`;
}
function renderTopBettors() {
  const box = $("#top-body"); if (!box) return;
  const tt = state.snapshot && state.snapshot.picks && state.snapshot.picks.top_traders;
  if (!tt) { box.innerHTML = `<div class="muted" style="padding:14px">Top bettors are tracked on the NFL, college football, NHL and MLB boards.</div>`; return; }
  const m = tt.meta || {};
  const upd = m.updated ? new Date(m.updated * 1000).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }) : "-";
  const head = `<div class="cal-note muted">${ico("smart")} <span>Tracking the <b>${m.poly_traders || 0}</b> Polymarket wallets in the top 100 sports traders by profit (this month and all-time) and the <b>${m.kalshi_traders || 0}</b> Kalshi traders in its top 100 (all-time and last 30 days), of whom <b>${m.kalshi_visible || 0}</b> share their holdings. Positions as of ${upd}, refreshed every 20 minutes. Big traders tend to build positions close to game time, so the board fills in as games approach.</span></div>`;
  const games = tt.games || [];
  const quiet = Math.max(0, (m.games_listed || 0) - games.length);
  const grid = games.length
    ? `<div class="pick-section"><h3>${ico("smart")} By game <span class="muted">· ${games.length} game${games.length > 1 ? "s" : ""} with top-bettor positions${quiet ? `, ${quiet} more with none yet` : ""}; tap a line to see who holds it</span></h3><div class="fgrid">${games.map(_topGame).join("")}</div></div>`
    : `<div class="muted" style="padding:14px">No top-bettor positions on this board's games yet. They usually build close to game time.</div>`;
  const lead = (tt.leaders || []).length ? `<div class="pick-section"><h3>${ico("track")} Who is active on this board <span class="muted">· leaderboard traders with money on these games</span></h3>
    <div class="tbl-scroll"><table class="flat"><thead><tr><th>Trader</th><th>Rank</th><th class="num">Profit</th><th class="num">Lines</th><th class="num">On these games</th></tr></thead><tbody>${tt.leaders.map((r) => `<tr>
      <td><span class="tb-plat ${r.platform === "Kalshi" ? "k" : "p"}">${r.platform === "Kalshi" ? "K" : "PM"}</span> <b title="${esc(r.name)}">${esc(_handle(r.name))}</b>${r.mm ? ` <span class="rchip cau">market maker?</span>` : ""}</td>
      <td>#${r.rank} <span class="muted">${esc(r.window)}</span></td><td class="num">${_money(r.pnl)}</td><td class="num">${r.lines}</td><td class="num">${_money(r.stake)}</td></tr>`).join("")}</tbody></table></div></div>` : "";
  box.innerHTML = head + grid + lead;
}

// ---------- render: Research (AI verdict detail + candidate browse) ----------
function renderResearch() {
  const s = state.snapshot; if (!s) return;
  const p = s.picks || {}, m = s.meta;
  state.propIndex = {};   // rebuilt by the prop tables below; the Read handler reads it
  const ai = p.ai || {};
  const aiCount = Object.keys(ai).length;

  let intro = "";
  if (!m.ai_enabled) {
    intro = "";
  } else if (!aiCount) {
    intro = `<div class="banner warn">${ico("analyze")}<span><b>No analysis yet.</b> Tap <b>Analyze</b> (top-right) for reasoned verdicts on today's favorites — a few cents, cached for the day.</span></div>`;
  }
  const cards = aiCount ? `<h2 class="ai-h">${ico("analyze")} AI slate analysis</h2>` + aiCardsHTML(ai) : "";
  const ml = p.model_ledger || {};
  const study = _researchStudyHTML(ml.research_study, ml.research_catalog);
  $("#research-body").innerHTML = intro + cards + study + cornersHTML(p.corners) + kalshiPropsHTML(p.kalshi_props) + browseHTML(p);
}

// de-vigged Kalshi player props (free): the sharp fair probability on each line, research-only
function kalshiPropsHTML(rows) {
  if (!rows || !rows.length) return "";
  const shown = rows.slice(0, 40);
  const tr = (r) => `<tr>
    <td><b>${esc(r.player)}</b></td>
    <td>${esc(r.stat)} <b>${r.line != null ? Math.ceil(r.line) : ""}+</b></td>
    <td class="num">${Math.round(r.over_fair * 100)}%</td>
    <td class="num">${sgn(fairAm(r.over_fair))} / ${sgn(fairAm(1 - r.over_fair))}</td>
    <td class="muted">${r.days_out != null ? dateLabel(r.days_out) : ""}</td></tr>`;
  const total = ((state.snapshot || {}).picks || {}).kalshi_props_total || rows.length;
  const more = total > shown.length ? `the ${shown.length} soonest of ${total} lines; ` : "";
  return `<div class="pick-section"><h3>${ico("markets")} Player props <span class="muted">· ${more}Kalshi's fair price with the vig removed; hold your book's price on the same line against it</span></h3>
    <div class="tbl-scroll"><table class="flat"><thead><tr><th>Player</th><th>Prop</th><th class="num">Fair</th><th class="num">Fair odds (over / under)</th><th>When</th></tr></thead>
    <tbody>${shown.map(tr).join("")}</tbody></table></div></div>`;
}


function cornersHTML(rows) {
  if (!rows || !rows.length) return "";
  const body = rows.map((r) => {
    const hasLine = r.line != null;
    const evpct = r.ev != null ? (r.ev > 0 ? "+" : "") + (r.ev * 100).toFixed(1) + "%" : "—";
    const evcls = r.ev >= 0.04 ? "flow-up" : r.ev < 0 ? "flow-dn" : "muted";
    const bettable = hasLine && r.ev >= 0.04 && r.confidence !== "prior";
    const lean = hasLine
      ? `<span class="readlean ${bettable ? "read-over" : "read-neutral"}">${esc(r.side)} ${r.line}</span>`
      : `<span class="muted">— pull line —</span>`;
    const conf = r.confidence === "high" ? `<span class="tag">measured</span>`
      : r.confidence === "medium" ? `<span class="tag" title="few games of corner data">thin</span>`
      : `<span class="tag" title="no corner history yet — pure league baseline">prior</span>`;
    return `<tr>
      <td><b>${esc(r.fav_team)}</b> <span class="muted">v</span> ${esc(r.opp_team)}<div class="muted">${r.days_out != null ? dateLabel(r.days_out) : ""}</div></td>
      <td class="num"><b>${r.proj_total}</b> <span class="muted">total</span><div class="muted">${r.proj_fav} – ${r.proj_opp}</div></td>
      <td class="num">${hasLine ? "O/U " + r.line + (r.book ? `<div class="muted">${esc(r.book)}</div>` : "") : "—"}</td>
      <td>${lean}</td>
      <td class="num ${evcls}">${evpct}</td>
      <td>${conf}</td></tr>`;
  }).join("");
  return `<div class="pick-section corners-section"><h3>${ico("value")} Corners <span class="muted">· model total vs book line · a dominance market</span></h3>
    <table><thead><tr><th>Match</th><th class="num">Projection</th><th class="num">Line</th><th>Lean</th><th class="num">EV</th><th>Conf</th></tr></thead><tbody>${body}</tbody></table>
    <div class="cal-note">${ico("shield")}<span>Projections use each team's corners-for/against shrunk to a league prior, adjusted for the opponent + projected possession. <b>prior</b> = no corner history yet (read, not bet). Tap <b>Odds</b> (left rail) to pull book lines for today/tomorrow (~1 credit/game); +EV corners with measured history then surface in <b>Best Bets</b> and log on <b>Analyze</b> like any other pick.</span></div></div>`;
}

function aiCardsHTML(ai) {
  const matches = Object.keys(ai || {});
  if (!matches.length) return "";
  matches.sort((a, b) => (ai[b].confidence || 0) - (ai[a].confidence || 0));
  return matches.map((mk) => {
    const v = ai[mk];
    const betSel = new Set((v.recommended_bets || []).map((b) => (b.selection || "").toLowerCase()));
    const bets = (v.recommended_bets || []).map((b) =>
      `<div class="ai-bet"><span class="who"><span class="tag">${esc(b.archetype.replace(/_/g, " "))}</span> <b>${esc(b.selection)}</b><div class="muted">${mdLite(b.rationale)}</div></span></div>`).join("")
      || `<div class="muted">No bet here — pass.</div>`;
    const fades = (v.fades || []).filter((f) => !betSel.has((f.selection || "").toLowerCase())).map((f) =>   // never both bet + fade
      `<div class="ai-fade">${ico("fade")}<span><b>${esc(f.selection)}</b> <span class="muted">${mdLite(f.why)}</span></span></div>`).join("");
    const sgp = (v.sgp_legs || []).length
      ? `<div class="ai-sgp">${ico("sgp")} SGP: ${v.sgp_legs.map(esc).join(" + ")}</div>` : "";
    const research = v.research
      ? `<details class="ai-research"><summary>${ico("chevron", "ico ico-chev")} situational brief — web-sourced</summary><div class="body">${mdLite(v.research)}</div></details>` : "";
    return `<div class="card ai-card">
      <div class="ai-top">
        <div><h3>${esc(mk)}</h3>${v.days_out != null ? `<div class="meta">${dateLabel(v.days_out)}</div>` : ""}</div>
        <div class="ai-rt">${verdictChip(v)}${confMeter(v.confidence)}</div>
      </div>
      <div class="ai-body">
        <div class="ai-headline">${mdLite((v.headline || "").length > 150 ? v.headline.slice(0, 148) + "…" : v.headline)}</div>
        ${v.memory_note ? `<div class="cal-note">${ico("track")} ${esc(v.memory_note)}</div>` : ""}
        ${bets}${fades}${sgp}
        ${smartLine(v)}
        <div class="ai-risk">${ico("shield")}<span><b>Risk:</b> ${esc(v.key_risk)}</span></div>
        ${research}
      </div>
    </div>`;
  }).join("");
}

function smartLine(v) {
  const sm = v.smart_money; if (!sm) return "";
  const part = (x) => x
    ? `${esc(x.team)} <span class="${x.flow === "buying" ? "flow-up" : x.flow === "selling" ? "flow-dn" : "muted"}">${esc(x.flow)}</span> ${x.net_flow_shares > 0 ? "+" : ""}${fmtPop(x.net_flow_shares)}`
    : "";
  const parts = [part(sm.favorite), part(sm.underdog)].filter(Boolean);
  return parts.length ? `<div class="ai-smart">${ico("smart")} <span class="muted">whale flow:</span> ${parts.join(" · ")}</div>` : "";
}

function browseHTML(p) {
  const meta = (state.snapshot || {}).meta || {};
  const soccer = meta.sport === "wc26";
  const hasModel = !!meta.model_loaded;
  const favRows = (p.favorite_ml || []).map((f) => `<tr class="${f.chalk ? "fav-chalk" : ""}">
    <td><b>${esc(f.team_key ? teamName(f.team_key) : f.team)}</b> ${f.chalk ? '<span class="tag chalk-tag">chalk</span>' : ""}</td>
    <td class="num">${Math.round(f.fair_prob * 100)}% <span class="muted">fair</span></td>
    ${hasModel ? `<td class="num">${f.model_prob != null ? Math.round(f.model_prob * 100) + "% model" : "—"}</td>` : ""}
    <td class="muted">${f.days_out != null ? `<span class="tag">${dateLabel(f.days_out)}</span> ` : ""}${esc(f.opp_key ? `vs ${teamName(f.opp_key)}` : f.event)}</td></tr>`).join("");
  const favTbl = (p.favorite_ml || []).length
    ? `<label class="chalk-toggle"><input type="checkbox" id="hide-chalk" ${document.body.classList.contains("hide-chalk") ? "checked" : ""}> Hide obvious chalk (80% or more)</label><div class="tbl-scroll"><table class="flat"><tbody>${favRows}</tbody></table></div>`
    : `<div class="empty">no clear favorites in the next few days</div>`;

  const ttLine = (p.team_total_over && p.team_total_over[0]) ? p.team_total_over[0].line : 1.5;
  const ttTbl = (p.team_total_over || []).length ? `<table><tbody>${p.team_total_over.map((t) => `<tr>
    <td><b>${esc(t.team)} Over ${t.line}</b></td>
    <td class="num">${(t.p_over * 100).toFixed(0)}% <span class="muted">model</span></td>
    <td class="num">${t.exp_goals != null ? t.exp_goals + " xG" : ""}</td>
    <td class="muted">${esc(t.event)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">none</div>`;

  // the soccer sections only exist on the World Cup board; an empty section is left out, not boxed
  const extra = soccer ? [
    ["Team total, over " + ttLine, "model expected goals", (p.team_total_over || []).length ? ttTbl : ""],
    ["Anytime goalscorer", "favorites' scorers first", (p.anytime_goalscorer || []).length ? propTable(p.anytime_goalscorer) : ""],
    ["Shots / shots on target, over", "by popularity", (p.shots_sot || []).length ? propTable(p.shots_sot) : ""],
    ["Popular props", "PrizePicks' most-played lines", (p.popular_props || []).length ? propTable(p.popular_props) : ""],
  ].filter((x) => x[2]).map((x) => pickSection(...x)).join("") : "";
  return `<details class="browse" open><summary>${ico("markets")} Favorites</summary><div class="browse-body">`
    + pickSection("Favorite moneylines", "clear favorites (55% or better)", favTbl) + extra
    + `</div></details>`;
}

// ---------- shared bits ----------
function oddsBadge(t) {
  if (!t || t === "standard") return "";
  return `<span class="ob ${t === "demon" ? "demon" : "goblin"}">${t}</span>`;
}
function readChip(rd) {
  if (!rd) return "";
  const m = { over: ["up", "Over", "read-over"], under: ["down", "Under", "read-under"],
              avoid: ["fade", "Avoid", "read-avoid"], neutral: ["pass", "No lean", "read-neutral"] };
  const [icon, txt, cls] = m[rd.lean] || m.neutral;
  const trap = rd.trap_risk ? `<span class="ob demon" title="trap risk">trap</span>` : "";
  return `<span class="readlean ${cls}" title="${esc(rd.rationale || "")}">${ico(icon)} ${txt}</span> ${trap}`;
}
function modelChip(p, v) {
  if (p == null) return `<span class="muted">—</span>`;
  const cls = v === "value" ? "mc-value" : v === "lean" ? "mc-lean" : v === "fade" ? "mc-fade" : "mc-none";
  return `<span class="mchip ${cls}">${Math.round(p * 100)}%</span>`;
}
function propTable(rows) {
  if (!rows || !rows.length) return `<div class="empty">nothing here</div>`;
  return `<table><thead><tr><th>Player</th><th>Bet</th><th>Match</th><th class="num">Popular</th><th class="num">Model</th><th>Read</th><th></th></tr></thead><tbody>${rows.map((r) => {
    const pid = `${r.player}|${r.stat_type}|${r.line}|${r.odds_type}`;
    state.propIndex[pid] = {
      player: r.player, team: r.team, position: r.position, stat_type: r.stat_type,
      line: r.line, odds_type: r.odds_type, popularity: r.popularity, opponent: r.opponent,
      on_favorite: r.on_favorite, match: `${r.team || ""} vs ${r.opponent || ""}`,
      ttover: r.ttover, fav_fair_pct: r.fav_fair_pct,
    };
    return `<tr>
    <td><b>${esc(r.player)}</b> ${r.on_favorite ? '<span class="favtag">fav</span>' : ""}<div class="muted">${esc(r.team || "")} ${esc(r.position || "")}</div></td>
    <td>${esc(r.stat_type)} <b>${r.line}</b> ${oddsBadge(r.odds_type)}</td>
    <td class="muted">vs ${esc(r.opponent || "")}</td>
    <td class="num"><span class="pop">${ico("pop")}${fmtPop(r.popularity)}</span></td>
    <td class="num">${modelChip(r.model_prob, r.model_value)}</td>
    <td>${readChip(r.read)}</td>
    <td><button class="act" data-pid="${esc(pid)}">Read</button></td></tr>`;
  }).join("")}</tbody></table>`;
}
function pickSection(title, sub, inner) {
  return `<div class="pick-section"><h3>${title} <span class="muted">· ${sub}</span></h3>${inner}</div>`;
}
function confMeter(n) {
  n = n || 0;
  let seg = "";
  for (let i = 1; i <= 5; i++) seg += `<i class="${i <= n ? "on" : ""}"></i>`;
  return `<span class="conf"><span class="label">Conf</span><span class="seg">${seg}</span></span>`;
}
function verdictChip(v) {
  const hasBets = (v.recommended_bets || []).length > 0;
  if (!hasBets) return `<span class="verdict pass">${ico("pass")} Pass</span>`;
  const label = (v.confidence || 0) >= 4 ? "Bet" : "Lean";
  return `<span class="verdict bet">${ico("check")} ${label}</span>`;
}

// ---------- render: Track Record (the one ledger; CLV-first proof) ----------
const fmtMoney = (n) => Math.round(n || 0).toLocaleString();
function sparkline(curve, w = 300, h = 52) {
  if (!curve || curve.length < 2) return `<div class="spark-empty">bankroll curve builds as picks settle</div>`;
  const min = Math.min(...curve), max = Math.max(...curve), range = (max - min) || 1, pad = 4;
  const pts = curve.map((v, i) => `${(i / (curve.length - 1)) * w},${h - pad - ((v - min) / range) * (h - pad * 2)}`);
  const up = curve[curve.length - 1] >= curve[0];
  const col = up ? "var(--positive)" : "var(--negative)";
  const area = `${pts.join(" ")} ${w},${h} 0,${h}`;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="spark-fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${col}" stop-opacity=".22"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    <polygon points="${area}" fill="url(#spark-fill)"/>
    <polyline points="${pts.join(" ")}" fill="none" stroke="${col}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
  </svg>`;
}
function bankrollHero(s) {
  const start = s.start_bankroll || 1000, bank = s.bankroll != null ? s.bankroll : start;
  const delta = bank - start, pctv = start ? (delta / start) * 100 : 0;
  const cls = delta > 0 ? "pos" : delta < 0 ? "neg" : "";
  const sign = delta >= 0 ? "+" : "−";
  const deltaTxt = `${sign}$${fmtMoney(Math.abs(delta))} · ${sign}${Math.abs(pctv).toFixed(1)}%`;
  return `<div class="bankroll-hero">
    <div class="bh-main">
      <div class="bh-label">Bankroll <span class="muted">· $${fmtMoney(s.unit_dollars || 10)}/unit</span></div>
      <div class="bh-val">$${fmtMoney(bank)}</div>
      <div class="bh-delta ${cls}">${delta === 0 ? "even" : deltaTxt} <span class="muted">from $${fmtMoney(start)}</span></div>
    </div>
    <div class="bh-spark">${sparkline(s.bankroll_curve)}</div>
  </div>`;
}
function renderPaper() {
  const d = state.paper; if (!d) return;
  const o = d.summary.overall;
  const stat = (label, val, cls = "") => `<div class="stat"><div class="label">${label}</div><div class="val ${cls}">${val}</div></div>`;
  const clvCls = o.avg_clv == null ? "" : o.avg_clv > 0 ? "pos" : "neg";
  const plCls = o.units_pl > 0 ? "pos" : o.units_pl < 0 ? "neg" : "";
  let summary =
    stat("Picks", o.picks) +
    stat("Avg CLV", o.avg_clv == null ? "—" : sgn(o.avg_clv) + "%", clvCls) +
    stat("Beat close", o.beat_close_pct == null ? "—" : o.beat_close_pct + "%", o.beat_close_pct >= 50 ? "pos" : "") +
    stat("Record", o.settled ? `${o.wins}-${o.settled - o.wins}` : "—") +
    // P/L and ROI count only the priced picks (moneylines, parlays); props carry no price
    stat(`Units P/L${o.priced != null ? ` <span class="muted">· ${o.priced} priced</span>` : ""}`, o.units_pl ? sgn(o.units_pl) + "u" : "—", plCls) +
    stat(`ROI${o.priced != null ? ` <span class="muted">· ${o.priced} priced</span>` : ""}`, o.roi_pct == null ? "—" : o.roi_pct + "%", plCls);
  if (d.summary.real_count) {
    const r = d.summary.real;
    summary += stat("Real $ P/L", r.units_pl ? sgn(r.units_pl) + "u" : "—", r.units_pl > 0 ? "pos" : r.units_pl < 0 ? "neg" : "");
  }
  $("#track-summary").innerHTML = bankrollHero(d.summary) + summary;

  // calibration — gated; show real buckets only once any clears n>=20, else an honest progress note
  const cal = (state.snapshot && state.snapshot.picks && state.snapshot.picks.calibration) || [];
  const gated = cal.filter((r) => r.gated);
  const calBox = $("#track-cal");
  if (calBox) {
    if (gated.length) {
      calBox.innerHTML = `<div class="pick-section"><h3>Calibration <span class="muted">· nudges confidence once a bucket clears 20 (CLV-first, never excludes a pick)</span></h3>
        <table><thead><tr><th>Context bucket</th><th class="num">Sample</th><th>Status</th><th class="num">Signal</th></tr></thead><tbody>${
        gated.map((r) => `<tr>
          <td><span class="tag">${esc(r.dim)}</span> ${esc(String(r.val).replace(/_/g, " "))}</td>
          <td class="num">${r.n_eff}</td>
          <td><span class="ev pos">active</span></td>
          <td class="num">${r.score == null ? "—" : (r.metric === "clv" ? sgn(r.score) + "% CLV" : sgn(r.score) + "pp")}</td></tr>`).join("")}</tbody></table></div>`;
    } else {
      const best = cal.reduce((mx, r) => Math.max(mx, r.n_eff || 0), 0);
      calBox.innerHTML = `<div class="cal-note">${ico("track")}<span>Confidence calibration unlocks once a context bucket reaches 20 settled picks. Best bucket so far: <b>${Math.round(best)}/20</b>.</span></div>`;
    }
  }

  // by-archetype
  const arch = d.summary.by_archetype;
  const akeys = Object.keys(arch);
  const archHtml = akeys.length ? `<div class="pick-section"><h3>By archetype</h3><table><thead><tr>
    <th>Archetype</th><th class="num">Picks</th><th class="num">Record</th><th class="num">Hit%</th>
    <th class="num">Avg CLV</th><th class="num">Beat close</th><th class="num">ROI</th></tr></thead><tbody>${
    akeys.map((k) => { const a = arch[k]; return `<tr>
      <td><span class="tag">${esc(k.replace(/_/g, " "))}</span></td>
      <td class="num">${a.picks}</td>
      <td class="num">${a.settled ? a.wins + "-" + (a.settled - a.wins) : "—"}</td>
      <td class="num">${a.hit_rate == null ? "—" : a.hit_rate + "%"}</td>
      <td class="num ${a.avg_clv > 0 ? "ev pos" : ""}">${a.avg_clv == null ? "—" : sgn(a.avg_clv) + "%"}</td>
      <td class="num">${a.beat_close_pct == null ? "—" : a.beat_close_pct + "%"}</td>
      <td class="num">${a.roi_pct == null ? "—" : a.roi_pct + "%"}</td></tr>`; }).join("")}</tbody></table></div>` : "";
  // model calibration: the model's projected P(hit) vs actual outcomes (fills as new picks settle)
  // a calibration row on two or three picks is noise, not a finding: only buckets of 20 or more show
  const mc = d.summary.model_calibration || {};
  const mck = Object.keys(mc).filter((k) => (mc[k].n || 0) >= 20);
  const calTbl = mck.length ? `<div class="pick-section"><h3>Model calibration <span class="muted">· projected vs actual once picks settle · Brier lower = sharper (0.25 = coin flip)</span></h3>
    <table><thead><tr><th>Archetype</th><th class="num">n</th><th class="num">Model says</th><th class="num">Actual</th><th class="num">Gap</th><th class="num">Brier</th></tr></thead><tbody>${
    mck.map((k) => { const m = mc[k]; const over = m.gap_pp > 5, under = m.gap_pp < -5; return `<tr>
      <td><span class="tag">${esc(k.replace(/_/g, " "))}</span></td>
      <td class="num">${m.n}</td>
      <td class="num">${Math.round(m.mean_pred * 100)}%</td>
      <td class="num">${Math.round(m.hit_rate * 100)}%</td>
      <td class="num ${over ? "neg" : ""}">${sgn(m.gap_pp)}pp${over ? " over" : under ? " under" : ""}</td>
      <td class="num">${m.brier}</td></tr>`; }).join("")}</tbody></table>
    <div class="cal-note">${ico("track")}<span>A large positive gap (model says more than actual) means the model over-projects that archetype.</span></div></div>` : "";
  $("#track-arch").innerHTML = archHtml + calTbl;

  // pick ledger — split Open / Settled, ordered by kickoff
  const body = $("#track-body");
  if (!d.picks.length) {
    const aiOn = ((state.snapshot || {}).meta || {}).ai_enabled;
    body.innerHTML = `<div class="empty">${ico("track")}<div>${aiOn ? "No picks logged yet. Tap <b>Analyze</b>: every recommended bet logs here and favorite moneylines settle themselves." : "No AI picks on this board. Its graded record is the <b>Model Ledger</b>, your own bets are under <b>Bet Slip</b>, and the weekend tickets under <b>Lotto</b>."}</div></div>`;
    return;
  }
  const dt = new Date();
  // use SERVER date (snapshot.generated_at), not the browser's, so a user east of the event-local
  // date bucket doesn't see not-yet-played games flip to "over" after local midnight. game_over (set
  // from real results) is the primary signal; the date check only catches games >1 day past.
  const srvToday = ((state.snapshot && state.snapshot.generated_at) || "").slice(0, 10)
    || `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
  const yesterday = new Date(srvToday + "T00:00:00"); yesterday.setDate(yesterday.getDate() - 1);
  const cutoff = `${yesterday.getFullYear()}-${String(yesterday.getMonth() + 1).padStart(2, "0")}-${String(yesterday.getDate()).padStart(2, "0")}`;
  const over = (p) => { const cd = (p.commence_time || "").slice(0, 10); return !!p.game_over || (!!cd && cd <= cutoff); };
  // sort by kickoff date, then GROUP every bet of the same game together (was scattering a game's
  // legs by log order), then log time within the game.
  const kick = (p) => (p.kickoff || p.commence_time || "9999") + "|" + (p.match || "") + "|" + (p.logged_at || "");
  const groups = {
    open: d.picks.filter((p) => p.status === "pending" && !over(p)).sort((a, b) => kick(a).localeCompare(kick(b))),      // upcoming, soonest first
    awaiting: d.picks.filter((p) => p.status === "pending" && over(p)).sort((a, b) => kick(b).localeCompare(kick(a))),   // finished, ungraded, recent first
    settled: d.picks.filter((p) => p.status !== "pending").sort((a, b) => kick(b).localeCompare(kick(a))),              // graded, recent first
  };
  // open on the first list that has picks (a finished event has none open), unless one was chosen
  const tab = state.trackChosen && groups[state.trackTab] ? state.trackTab
    : ["open", "awaiting", "settled"].find((k) => groups[k].length) || "open";
  const chip = (k, label) => `<button class="dchip ${tab === k ? "on" : ""}" data-track="${k}">${label} ${groups[k].length}</button>`;
  const chips = `<div class="day-filter">${chip("open", "Open")}${chip("awaiting", "Awaiting")}${chip("settled", "Settled")}</div>`;
  const hint = tab === "awaiting"
    ? `<p class="hint" style="margin:-2px 0 12px">Finished games not yet graded — MLs &amp; your A+B+D parlay auto-settle; grade the prop legs (shots, passes) yourself to feed calibration.</p>` : "";
  const list = groups[tab];
  const stream = list.length
    ? `<div class="track-stream">${list.map(trackCard).join("")}</div>`
    : `<div class="empty">no ${tab} picks</div>`;
  body.innerHTML = `<div class="pick-section"><h3>Pick ledger</h3>${chips}${hint}${stream}</div>`;
  body.querySelectorAll("[data-pstatus]").forEach((sel) => sel.onchange = async () => {
    await getJSON("api/paper/" + sel.dataset.pstatus, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: sel.value }) });
    loadPaper();
  });
  body.querySelectorAll("[data-pmoney]").forEach((cb) => cb.onchange = async () => {
    await getJSON("api/paper/" + cb.dataset.pmoney, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ real_money: cb.checked ? 1 : 0 }) });
    loadPaper();
  });
  body.querySelectorAll("[data-pdel]").forEach((b) => b.onclick = async () => {
    await getJSON("api/paper/" + b.dataset.pdel, { method: "DELETE" });
    loadPaper();
  });
}

function kickoffLabel(p) {
  if (p.kickoff && /T/.test(p.kickoff) && !p.kickoff.startsWith("9999")) {   // skip the end-of-day sentinel
    const d = new Date(p.kickoff);
    if (!isNaN(d)) return d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  return p.commence_time ? p.commence_time.slice(5, 10) : "";
}

function trackCard(p) {
  const stCls = p.status === "won" ? "won" : p.status === "lost" ? "lost" : "";
  // CLV only applies to bets with a closing-line metric (moneylines) — don't show "CLV —" noise on props
  const clv = p.clv_pct == null ? "" : `<span class="ev ${p.clv_pct > 0 ? "pos" : ""}">CLV ${sgn(p.clv_pct)}%</span>`;
  const settled = p.closing_locked_at ? ` <span class="tag">auto-settled</span>` : "";
  // build the meta as clean dot-separated segments — omit entry→close for non-CLV picks
  const segs = [esc(p.match)];
  const when = kickoffLabel(p);
  if (when) segs.push(when);
  if (p.pick_fair_prob != null) {
    segs.push(`entry ${(p.pick_fair_prob * 100).toFixed(0)}%`
      + (p.closing_fair_prob != null ? ` → close ${(p.closing_fair_prob * 100).toFixed(0)}%` : ""));
  }
  segs.push(`${p.stake_units || 1}u`);
  const pl = (p.units_pl != null && (p.status === "won" || p.status === "lost"))
    ? ` · <span class="ev ${p.units_pl > 0 ? "pos" : p.units_pl < 0 ? "neg" : ""}">${sgn(p.units_pl)}u</span>` : "";
  return `<div class="card track-card ${stCls}">
    <div class="tc-top"><span class="tag">${esc(p.archetype.replace(/_/g, " "))}</span><b>${esc(p.selection)}</b><span class="bc-r">${clv}</span></div>
    <div class="tc-meta muted">${segs.join(" · ")}${pl}${settled}</div>
    <div class="tc-actions">
      <select data-pstatus="${p.id}">${["pending", "won", "lost", "void"].map((x) => `<option ${x === p.status ? "selected" : ""}>${x}</option>`).join("")}</select>
      <label class="rm"><input type="checkbox" data-pmoney="${p.id}" ${p.real_money ? "checked" : ""}> real money</label>
      <button class="act" data-pdel="${p.id}">remove</button>
    </div>
  </div>`;
}

// ---------- tabs / events ----------
// ---------- the board switcher: every sport on one site (/nfl/, /cfb/, ...), from the snapshot's board list ----------
function renderBoards(meta) {
  const box = document.getElementById("boards");
  const list = (meta && meta.boards) || [];
  const here = list.find((b) => b.sport === meta.sport);
  // only on the one site: a board served on its own (local development) has no siblings at /<path>/
  if (!box || !here || !location.pathname.startsWith("/" + here.path + "/")) { if (box) box.hidden = true; return; }
  const tab = state.tab && state.tab !== "picks" ? "#" + state.tab : "";
  box.hidden = false;
  box.innerHTML = `<div class="nav-group">Sport</div><div class="board-row">${list.map((b) => {
    const n = b.upcoming;
    const title = `${b.name}${n ? ` · ${n} game${n === 1 ? "" : "s"} coming up` : n === 0 ? " · no games coming up" : ""}`;
    return `<a class="board${b.sport === meta.sport ? " active" : ""}${n ? "" : " quiet"}" href="/${esc(b.path)}/${b.sport === meta.sport ? "" : tab}" title="${esc(title)}" aria-label="${esc(title)}">${esc(b.code)}</a>`;
  }).join("")}</div><div class="board-name">${esc(here.name)}</div><div class="nav-group">Board</div>`;
}

function switchTab(name) {
  state.tab = name;
  try { history.replaceState(null, "", name === "picks" ? location.pathname + location.search : "#" + name); } catch (e) { /* file:// or sandboxed */ }
  if (state.snapshot) renderBoards(state.snapshot.meta);
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "track") loadPaper();
  if (name === "slip") loadMyBets();
}
document.querySelectorAll(".tab").forEach((t) => t.onclick = () => switchTab(t.dataset.tab));

$("#refresh").onclick = () => loadSnapshot(true, false);   // free feeds only
$("#refresh-odds").onclick = () => {
  const left = state.snapshot?.meta?.odds?.credits_remaining;
  const msg = "Fetch fresh sportsbook lines (DraftKings/FanDuel/etc.)?\n\nPulls moneylines for the whole slate (1 credit) plus total-corner lines for today & tomorrow's games (~1 credit each)" +
    (left != null ? ` — ${left} of 500 monthly credits left.` : ".") + "\n\n+EV corners then show up in Best Bets like any other pick.";
  if (confirm(msg)) loadSnapshot(false, true);
};
$("#refresh-ai").onclick = () => {
  if (confirm("Run web-grounded AI analysis on today's favorite matches?\n\nEach match gets a live web-search brief (form, injuries, lineups, rotation risk) + a reasoned verdict. Uses your Anthropic key — roughly 25–75¢ for a full slate. Matches already analyzed today are cached and cost nothing.")) {
    loadSnapshot(false, false, true);
  }
};

// per-prop AI "Read" button (delegated; ~1¢, cached) — lives on the Research tab
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-pid]");
  if (!btn) return;
  const payload = state.propIndex[btn.getAttribute("data-pid")];
  if (!payload) return;
  btn.textContent = "…"; btn.disabled = true;
  try {
    const r = await getJSON("api/propread", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const cls = r.lean === "over" ? "read-over" : (r.lean === "under" || r.lean === "avoid") ? "read-avoid" : "read-neutral";
    const conf = r.confidence ? ` ${r.confidence}/5` : "";
    const det = el(`<tr class="detail"><td colspan="7"><span class="readlean ${cls}">AI: ${esc(r.lean)}${conf}</span> <span class="muted">${esc(r.why || "")}</span>${r.source === "haiku" && !r.cached ? ' <span class="src pm">live</span>' : ""}</td></tr>`);
    btn.closest("tr").after(det); btn.style.display = "none";
  } catch (err) { btn.textContent = "Read"; btn.disabled = false; }
});

// Futures tab: persist scouting notes; per-row AI Read; log/remove a lean (delegated)
document.addEventListener("input", (e) => {
  if (e.target.id === "scout-notes") localStorage.setItem("overlay_futures_notes", e.target.value);
});
document.addEventListener("click", async (e) => {
  const rd = e.target.closest("[data-fread]");
  if (rd) {
    rd.textContent = "…"; rd.disabled = true;
    const payload = { team: rd.dataset.team, kind: rd.dataset.kind,
      market_pct: parseFloat(rd.dataset.mk), model_pct: parseFloat(rd.dataset.md),
      record: rd.dataset.rec, notes: scoutNotes() };
    try {
      const r = await getJSON("api/futuresread", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      const cls = r.lean === "back" ? "read-over" : r.lean === "fade" ? "read-avoid" : "read-neutral";
      const conf = r.confidence ? ` ${r.confidence}/5` : "";
      const det = el(`<tr class="detail"><td colspan="5"><span class="readlean ${cls}">AI: ${esc(r.lean)}${conf}</span> <span class="muted">${esc(r.why || "")}</span>${r.source === "haiku" && !r.cached ? ' <span class="src pm">live</span>' : ""}</td></tr>`);
      rd.closest("tr").after(det); rd.style.display = "none";
    } catch (err) { rd.textContent = "Read"; rd.disabled = false; }
    return;
  }
  const ln = e.target.closest("[data-flean]");
  if (ln) {
    const note = prompt(`Optional note for your ${ln.dataset.dir} on ${teamName(ln.dataset.team)} (${ln.dataset.kind}):`, "");
    if (note === null) return;   // cancelled
    ln.disabled = true;
    try {
      await getJSON("api/futures/lean", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ team: ln.dataset.team, kind: ln.dataset.kind, direction: ln.dataset.dir, entry_pct: parseFloat(ln.dataset.pct), note: note.trim() }) });
      await loadSnapshot();
    } catch (err) { ln.disabled = false; }
    return;
  }
  const rm = e.target.closest("[data-lean-rm]");
  if (rm) {
    rm.disabled = true;
    try { await getJSON("api/futures/lean/" + encodeURIComponent(rm.dataset.leanRm), { method: "DELETE" }); await loadSnapshot(); }
    catch (err) { rm.disabled = false; }
    return;
  }
  const fv = e.target.closest("[data-fview]");
  if (fv) { localStorage.setItem("overlay_futures_view", fv.dataset.fview); renderFutures(); return; }
  const sc = e.target.closest("[data-scen-clear]");
  if (sc) { state.scenario = { pins: [], deltas: [] }; _savePins(); renderFutures(); return; }
  const pn = e.target.closest("[data-pin-w]");
  if (pn) { toggleScenarioPin(pn.dataset.pinA, pn.dataset.pinB, pn.dataset.pinW); return; }
});

// scenario pins survive a refresh (pins only; deltas are relative to the sim baseline at POST time, so
// they are always recomputed against the CURRENT bracket, never restored stale)
function _savePins() {
  try { localStorage.setItem("overlay_scenario_pins", JSON.stringify((state.scenario || {}).pins || [])); }
  catch (err) { /* storage full/blocked: pins just won't persist */ }
}
function _loadPins() {
  try {
    const pins = JSON.parse(localStorage.getItem("overlay_scenario_pins") || "[]");
    if (Array.isArray(pins) && pins.length) { state.scenario.pins = pins; return true; }
  } catch (err) { /* corrupt entry: ignore */ }
  return false;
}
async function _refreshScenario() {
  try {
    const r = await getJSON("api/futures/scenario", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pins: state.scenario.pins }) });
    state.scenario.deltas = r.deltas || [];
    state.scenario.pins = (r.pins && r.pins.length) ? r.pins : state.scenario.pins;   // backend drops pins it can't apply
    _savePins();
    renderFutures();
  } catch (err) { /* keep the optimistic pins; deltas just stay empty */ }
}

// pin a knockout tie's winner and ask the backend how the model's deep-run odds shift. The pin is a
// hypothetical, so this only moves the model sim (the market can't be re-priced for a what-if).
async function toggleScenarioPin(a, b, winner) {
  if (!a || !b || !winner) return;
  state.scenario = state.scenario || { pins: [], deltas: [] };
  const same = (p) => (p.a === a && p.b === b) || (p.a === b && p.b === a);
  const existing = state.scenario.pins.find(same);
  if (existing && existing.winner === winner) {
    state.scenario.pins = state.scenario.pins.filter((p) => !same(p));   // tap the pinned team again to clear
  } else {
    state.scenario.pins = state.scenario.pins.filter((p) => !same(p)).concat([{ a, b, winner }]);
  }
  _savePins();
  renderFutures();   // optimistic: show the pin immediately, fill deltas when the sim returns
  if (!state.scenario.pins.length) { state.scenario.deltas = []; renderFutures(); return; }
  await _refreshScenario();
}

// hide-chalk toggle (delegated; persists across re-renders via the body class)
document.addEventListener("change", (e) => {
  if (e.target.id === "hide-chalk") document.body.classList.toggle("hide-chalk", e.target.checked);
});

// best-bets day filter (delegated)
document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-day]");
  if (!c) return;
  state.dayFilter = c.dataset.day === "all" ? null : Number(c.dataset.day);
  if (state.snapshot) renderBestBets(state.snapshot.picks || {});
});
// track-record Open/Settled toggle (delegated)
document.addEventListener("click", (e) => {
  const c = e.target.closest("[data-track]");
  if (!c) return;
  state.trackTab = c.dataset.track; state.trackChosen = true;
  if (state.paper) renderPaper();
});

// ---------- boot ----------
const _hadPins = _loadPins();   // restore scenario pins from a prior visit (deltas recompute fresh)
// a board address can carry its tab (/cfb/#lotto), so switching sports keeps you on the same tab
function _openHashTab() {
  let name = "";
  try { name = decodeURIComponent((location.hash || "").slice(1)); } catch (e) { return; }   // a mangled link
  const btn = name && document.querySelector(`.tab[data-tab="${CSS.escape(name)}"]`);
  if (btn && btn.style.display !== "none") switchTab(name);
}
// scenario pins belong to the World Cup bracket; every board shares one origin now, so only a board with
// futures refreshes them (the others would run a soccer bracket sim for a hidden tab)
const _hasFutures = () => (((state.snapshot || {}).meta || {}).capabilities || []).includes("futures");
loadSnapshot().then(() => { _openHashTab(); if (_hadPins && state.scenario.pins.length && _hasFutures()) _refreshScenario(); });
// auto-refresh, but never yank the UI out from under an open read: skip the cycle while the user has
// a situational brief or a per-prop Read row expanded (they'll get fresh data on the next tick).
setInterval(() => {
  if (document.querySelector(".ai-research[open], tr.detail")) return;
  if (document.activeElement && document.activeElement.id === "scout-notes") return;  // don't yank notes mid-type
  loadSnapshot();
}, 60000);
