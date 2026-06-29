"use strict";

const state = { snapshot: null, paper: null, tab: "picks", propIndex: {}, dayFilter: null, trackTab: "open", scenario: { pins: [], deltas: [] } };

// ---------- helpers ----------
const $ = (sel) => document.querySelector(sel);
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (p) => (p == null ? "—" : (p * 100).toFixed(1) + "%");
const sgn = (n) => (n > 0 ? "+" + n : "" + n);
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
    state.snapshot = await getJSON("/api/snapshot" + (qs.length ? "?" + qs.join("&") : ""));
    renderAll();
  } catch (e) {
    $("#meta-updated").textContent = "error: " + e.message.slice(0, 80);
  }
}
async function loadPaper() {
  state.paper = await getJSON("/api/paper");
  renderPaper();
}

// ---------- render: top-level ----------
function renderAll() {
  const s = state.snapshot; if (!s) return;
  const m = s.meta;
  $("#meta-updated").textContent = "updated " + s.generated_at;
  setPill("pill-pm", m.sources_live.polymarket);
  setPill("pill-kalshi", m.sources_live.kalshi);
  setPill("pill-books", m.sources_live.sportsbooks);
  setPill("pill-model", m.model_loaded);

  // sportsbook odds credit meter (rail footer)
  const od = m.odds || {};
  const oc = $("#odds-credits"), btn = $("#refresh-odds"), meter = $("#odds-meter"), fill = $("#odds-meter-fill");
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
  renderPicks();
  renderLedger();
  renderResearch();
  renderFutures();
  if (state.tab === "track") loadPaper();
}

const teamName = (k) => (k || "").replace(/\b\w/g, (c) => c.toUpperCase());

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
const _koLabel = (iso) => {
  if (!iso) return "TBD";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso.slice(0, 16));
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " +
    d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
};
const _trip = (p) => `${Math.round(p[0] * 100)} / ${Math.round(p[1] * 100)} / ${Math.round(p[2] * 100)}`;
// stacked 1X2 bar: team-A win | draw | team-B win
function _triBar(p, a, b) {
  const w = (x) => (Math.max(0, x) * 100).toFixed(1);
  return `<span class="tribar">`
    + `<i class="t-a" style="width:${w(p[0])}%" title="${esc(teamName(a))} win ${Math.round(p[0] * 100)}%"></i>`
    + `<i class="t-d" style="width:${w(p[1])}%" title="draw ${Math.round(p[1] * 100)}%"></i>`
    + `<i class="t-b" style="width:${w(p[2])}%" title="${esc(teamName(b))} win ${Math.round(p[2] * 100)}%"></i></span>`;
}
// the model's pick on each extra market, as a compact chip
function _legLabel(l) {
  const t = l.team ? teamName(l.team) + " " : "";
  if (l.key === "total_goals") return `Total ${l.side === "over" ? "O" : "U"}${l.line}`;
  if (l.key === "team_total") return `${t}${l.side === "over" ? "O" : "U"}${l.line}`;
  if (l.key === "btts") return `BTTS ${l.side === "yes" ? "Yes" : "No"}`;
  if (l.key === "corners") return `Corners ${l.side === "over" ? "O" : "U"}${l.line}`;
  return l.key;
}
function _legTitle(l, graded) {
  const bits = [];
  if (l.proj != null) bits.push(`model projects ${l.proj}`);
  if (graded) bits.push(l.result === "pending" ? "awaiting data" : `actual ${l.actual} (${l.result})`);
  else bits.push(`${Math.round((l.prob || 0) * 100)}% confidence`);
  return bits.join("; ");
}
function _legChips(legs, graded) {
  if (!legs || !legs.length) return "";
  const chips = legs.map((l) => {
    const r = l.result;
    const cls = graded ? (r === "won" ? "leg-won" : r === "lost" ? "leg-lost" : "leg-pend") : "";
    const tail = graded ? (r === "won" ? " ✓" : r === "lost" ? " ✗" : " ·")
      : ` <span class="legpct">${Math.round((l.prob || 0) * 100)}%</span>`;
    return `<span class="leg ${cls}" title="${esc(_legTitle(l, graded))}">${esc(_legLabel(l))}${tail}</span>`;
  }).join("");
  return `<div class="legs">${chips}</div>`;
}
// per-market hit rate across settled games (only counts legs that actually graded)
function _legAccuracy(settled) {
  const names = { total_goals: "Total goals", team_total: "Team totals", btts: "BTTS", corners: "Corners" };
  const tally = {};
  settled.forEach((r) => (r.legs || []).forEach((l) => {
    if (l.result !== "won" && l.result !== "lost") return;
    const t = tally[l.key] || (tally[l.key] = { w: 0, n: 0 });
    t.n++; if (l.result === "won") t.w++;
  }));
  const cells = ["total_goals", "team_total", "btts", "corners"].filter((k) => tally[k])
    .map((k) => `<span class="leg-acc"><b>${names[k]}</b> ${tally[k].w}/${tally[k].n}</span>`);
  return cells.length ? `<div class="cal-note muted">Our picks vs result: ${cells.join(" · ")}</div>` : "";
}
function _forecastCard(c) {
  const badge = c.frozen
    ? `<span class="tag pin" title="frozen ${esc(c.lock_ts || "")}">locked</span>`
    : `<span class="tag" title="live model line, not yet part of the record">preview</span>`;
  // biggest model-vs-market disagreement across the three outcomes, flagged when it is material
  const gap = Math.round(Math.max(...[0, 1, 2].map((i) => Math.abs(c.model[i] - c.market[i]))) * 100);
  const gapChip = gap >= 8 ? ` <span class="tag gap" title="largest model vs market gap">Δ${gap}pp</span>` : "";
  return `<div class="fcard">
    <div class="fcard-h"><span class="fcard-m"><b>${esc(teamName(c.a))}</b> <span class="muted">v</span> <b>${esc(teamName(c.b))}</b></span><span class="fcard-k muted">${_koLabel(c.kickoff_iso)} ${badge}${gapChip}</span></div>
    <div class="fcard-leg muted"><span>${esc(teamName(c.a))}</span><span>Draw</span><span>${esc(teamName(c.b))}</span></div>
    <div class="fcard-row"><span class="fcard-t">Model</span>${_triBar(c.model, c.a, c.b)}<span class="fcard-n">${_trip(c.model)}</span></div>
    <div class="fcard-row"><span class="fcard-t">Market</span>${_triBar(c.market, c.a, c.b)}<span class="fcard-n">${_trip(c.market)}</span></div>
    ${_legChips(c.legs, false)}
  </div>`;
}
function _ledgerScore(s) {
  const open = s.locked_pending ? `<span class="muted">${s.locked_pending} locked, awaiting result</span>` : "";
  if (!s.ready) {
    return `<div class="cal-note">${ico("track")} <b>${s.n}</b> of ${s.min_n} graded forecasts. Aggregate scores stay hidden until ${s.min_n} settle, so a couple of games cannot masquerade as a verdict. Each game below is still graded on its own. ${open}</div>`;
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
  <div class="cal-note muted">${esc(verdict)}. Exploratory: ${s.n} settled is a small sample with wide error bars. This measures the model, it is not a betting record, and it must never be used to retune the model. ${open}</div>`;
}
function renderLedger() {
  const box = $("#ledger-body"); if (!box) return;
  const ml = state.snapshot && state.snapshot.picks && state.snapshot.picks.model_ledger;
  if (!ml) { box.innerHTML = `<div class="muted" style="padding:14px">The Model Ledger fills in as the knockout games approach.</div>`; return; }
  const rows = ml.rows || [];
  const locked = rows.filter((r) => r.status === "locked").map((r) => ({
    a: r.team_a, b: r.team_b, model: [r.model_a, r.model_draw, r.model_b], legs: r.legs,
    market: [r.market_a, r.market_draw, r.market_b], kickoff_iso: r.kickoff_iso, frozen: true, lock_ts: r.lock_ts }));
  const upcoming = (ml.upcoming || []).map((c) => ({
    a: c.team_a, b: c.team_b, model: c.model, market: c.market, legs: c.legs, kickoff_iso: c.kickoff_iso, frozen: false }));
  const settled = rows.filter((r) => r.status === "settled");
  const cards = locked.concat(upcoming);
  const cardHtml = cards.length
    ? `<div class="pick-section"><h3>${ico("markets")} Forecasts <span class="muted">· locked freezes the line ${ml.buffer_min || 75} min before kickoff; preview is the live model line until then</span></h3><div class="fgrid">${cards.map(_forecastCard).join("")}</div></div>`
    : "";
  let settledHtml = "";
  if (settled.length) {
    const r2 = (x) => (x == null ? "—" : x.toFixed(2));
    const row = (r) => {
      const m = [r.model_a, r.model_draw, r.model_b], k = [r.market_a, r.market_draw, r.market_b];
      const oc = r.actual_outcome === "a" ? r.team_a : r.actual_outcome === "b" ? r.team_b : "Draw";
      const score = `${r.actual_a}-${r.actual_b}${r.pens ? " (pens)" : ""}`;
      const better = r.brier_model < r.brier_market;
      const legsRow = (r.legs && r.legs.length)
        ? `<tr class="legrow"><td colspan="6">${_legChips(r.legs, true)}</td></tr>` : "";
      return `<tr>
        <td><b>${esc(teamName(r.team_a))}</b> <span class="muted">v</span> <b>${esc(teamName(r.team_b))}</b><div class="muted tiny">${esc((r.commence_time || "").slice(0, 10))}</div></td>
        <td>${_triBar(m, r.team_a, r.team_b)}<div class="fcard-n muted">${_trip(m)}</div></td>
        <td>${_triBar(k, r.team_a, r.team_b)}<div class="fcard-n muted">${_trip(k)}</div></td>
        <td><b>${esc(score)}</b><div class="muted tiny">${esc(r.actual_outcome === "draw" ? "draw" : teamName(oc) + " win")}</div></td>
        <td class="num ${better ? "flow-up" : "flow-dn"}">${r2(r.brier_model)}<span class="muted"> / ${r2(r.brier_market)}</span></td>
        <td class="num">${r.hit_model ? "✓" : "✗"}</td></tr>${legsRow}`;
    };
    settledHtml = `<div class="pick-section"><h3>${ico("track")} Graded <span class="muted">· the 1X2 row scores the model vs the market frozen at the same instant (lower Brier is better, green = model beat the market); the chips under each game are our pick on every other line, marked right or wrong</span></h3>
      ${_legAccuracy(settled)}
      <table class="lg-tbl"><thead><tr><th>Game</th><th>Model</th><th>Market</th><th>Result</th><th class="num">Brier m/mkt</th><th class="num">Hit</th></tr></thead><tbody>${settled.map(row).join("")}</tbody></table></div>`;
  }
  const empty = (!cards.length && !settled.length)
    ? `<div class="muted" style="padding:14px">No forecasts yet. Each knockout game is logged here and freezes about ${ml.buffer_min || 75} minutes before kickoff.</div>` : "";
  box.innerHTML = _ledgerScore(ml.summary || { n: 0, min_n: 8, ready: false }) + cardHtml + settledHtml + empty;
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
    + `<div class="cal-note">${ico("shield")} <b>Market</b> is the de-vigged Polymarket price, the sharp vig-free probability and the number to trust. Each team's % is its odds to win that tie and advance; the small <b>tie</b> line gives market vs model for the top team. Switch to <b>List</b> to tap <b>Read</b> (an AI take weighing your notes) and log <b>Back</b> / <b>Fade</b> leans.</div>`
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

function renderBracket(br) {
  if (!br || !br.rounds || !br.rounds.length) return `<div class="muted" style="padding:14px">The bracket fills in as the knockout markets price up.</div>`;
  const teamCell = (team, pct, win, mu) => {
    if (!team) return `<div class="bteam tbd">TBD</div>`;
    const clickable = mu && !mu.winner && mu.a && mu.b;
    const pinned = mu && _isPinned(mu, team);
    const attrs = clickable ? ` data-pin-a="${esc(mu.a)}" data-pin-b="${esc(mu.b)}" data-pin-w="${esc(team)}"` : "";
    return `<div class="bteam ${win ? "bwin" : ""} ${pinned ? "bpin" : ""} ${clickable ? "bclick" : ""}"${attrs}><span class="bname">${esc(teamName(team))}</span><span class="bpct">${pct == null ? "" : pct + "%"}</span></div>`;
  };
  const match = (mu) => {
    const tie = mu.mkt_a == null ? "" : `<div class="btie">mkt ${mu.mkt_a}% · mdl ${mu.mdl_a == null ? "—" : mu.mdl_a + "%"}</div>`;
    return `<div class="bmatch ${mu.winner ? "done" : "pending"}" title="${esc(matchupRead(mu))}">${teamCell(mu.a, mu.a_pct, mu.winner === mu.a, mu)}${teamCell(mu.b, mu.b_pct, mu.winner === mu.b, mu)}${tie}</div>`;
  };
  const cols = br.rounds.map((r) => `<div class="bcol"><div class="bcol-h">${esc(r.name)}</div>${r.matchups.map(match).join("")}</div>`).join("");
  const champ = br.champion ? `<div class="bcol bcol-champ"><div class="bcol-h">Champion</div><div class="bmatch champ">${teamCell(br.champion, null, true)}</div></div>` : "";
  const hint = `<div class="muted bhint">Tap a team in an undecided tie to pin them through and see the bracket shift.</div>`;
  return `<div class="bracket-wrap"><div class="bracket">${cols}${champ}</div></div>${hint}`;
}
function setPill(id, on) { $("#" + id).classList.toggle("live", !!on); }

// ---------- render: Best Bets (the hero, cards-first) ----------
function renderPicks() {
  const s = state.snapshot; if (!s) return;
  const p = s.picks || {}, m = s.meta;
  const ab = $("#ai-banner");
  const aiCount = Object.keys(p.ai || {}).length;
  if (!m.ai_enabled) {
    ab.className = "banner warn";
    ab.innerHTML = `${ico("shield")}<span><b>AI analysis is off.</b> Add <code>ANTHROPIC_API_KEY</code> to <code>.env</code> for reasoned bet / pass / <b>fade</b> verdicts. Showing heuristic reads + model favorites for now.</span>`;
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

function renderBestBets(p) {
  const box = $("#bestbets"), bb = p.best_bets || [];
  if (!bb.length) {
    box.innerHTML = `<div class="empty">${ico("picks")}<div>No bets surfaced yet — tap Analyze for AI picks, or open the <b>Research</b> tab to browse candidates.</div></div>`;
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

  box.innerHTML = `<h2 class="ai-h">${ico("picks")} Best bets <span class="muted">· ranked, with reasoning</span></h2>${chips}${grid}${fadeHtml}`;
}

function betCard(c) {
  const srcLabel = { ai: "AI reasoned", read: "quick read", model: "model" }[c.source] || c.source;
  const right = c.confidence ? confMeter(c.confidence) : (c.tier ? `<span class="tier tier-${c.tier}">${c.tier}</span>` : "");
  const trap = c.trap ? '<span class="ob demon">trap</span>' : "";
  const price = c.best_american != null
    ? `<div class="bc-price">${ico("best")} best <b>${sgn(c.best_american)}</b> @ ${esc(c.best_book)}${c.ev_pct != null ? ` <span class="ev ${c.ev_pct > 0 ? "pos" : c.ev_pct < 0 ? "neg" : ""}">${c.ev_pct > 0 ? "+" : ""}${c.ev_pct}% vs fair</span>` : ""}</div>`
    : "";
  const model = c.model_prob != null
    ? `<div class="bc-model">${ico("track")} model <b>${Math.round(c.model_prob * 100)}%</b> to hit ${modelChip(c.model_prob, c.model_value)}</div>` : "";
  const stake = c.stake_units
    ? `<div class="bc-stake">${ico("odds")} stake <b>${c.stake_units}u</b> <span class="muted">· $${c.stake_dollars}</span></div>` : "";
  const mem = c.memory_note ? `<div class="cal-note">${ico("track")} ${esc(c.memory_note)}</div>` : "";
  const research = c.research
    ? `<details class="ai-research"><summary>${ico("chevron", "ico ico-chev")} situational brief</summary><div class="body">${mdLite(c.research)}</div></details>` : "";
  return `<div class="card bet-card">
    <div class="bc-top"><span class="tag">${esc((c.archetype || "").replace(/_/g, " "))}</span><span class="bc-src ${c.source}">${srcLabel}</span><span class="bc-r">${right}</span></div>
    <div class="bc-sel">${esc(c.selection)} ${trap}</div>
    <div class="bc-meta">${esc(c.match || "")}${c.days_out != null ? " · " + dateLabel(c.days_out) : ""}</div>
    <div class="bc-why">${mdLite(c.reasoning || "")}</div>
    ${price}${model}${stake}${mem}${research}
  </div>`;
}

function fadeCard(f) {
  return `<div class="card fade-card">
    <div class="fc-top">${ico("fade")}<b>${esc(f.selection)}</b></div>
    <div class="bc-meta">${esc(f.match || "")}${f.days_out != null ? " · " + dateLabel(f.days_out) : ""}</div>
    <div class="bc-why">${esc(f.reasoning || "")}</div>
  </div>`;
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
    intro = `<div class="banner warn">${ico("shield")}<span><b>AI analysis is off.</b> Add <code>ANTHROPIC_API_KEY</code> to <code>.env</code> for web-grounded bet / pass / <b>fade</b> verdicts. The candidate browse below still works (deterministic reads).</span></div>`;
  } else if (!aiCount) {
    intro = `<div class="banner warn">${ico("analyze")}<span><b>No analysis yet.</b> Tap <b>Analyze</b> (top-right) for reasoned verdicts on today's favorites — a few cents, cached for the day.</span></div>`;
  }
  const cards = aiCount ? `<h2 class="ai-h">${ico("analyze")} AI slate analysis</h2>` + aiCardsHTML(ai) : "";
  $("#research-body").innerHTML = intro + cards + cornersHTML(p.corners) + browseHTML(p);
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
    <div class="cal-note">${ico("shield")} Projections use each team's corners-for/against shrunk to a league prior, adjusted for the opponent + projected possession. <b>prior</b> = no corner history yet (read, not bet). Tap <b>Odds</b> (left rail) to pull book lines for today/tomorrow (~1 credit/game); +EV corners with measured history then surface in <b>Best Bets</b> and log on <b>Analyze</b> like any other pick.</div></div>`;
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
  const favRows = (p.favorite_ml || []).map((f) => `<tr class="${f.chalk ? "fav-chalk" : ""}">
    <td><b>${esc(f.team)}</b> ${f.chalk ? '<span class="tag chalk-tag">chalk</span>' : ""}</td>
    <td class="num">${(f.fair_prob * 100).toFixed(0)}% <span class="muted">fair</span></td>
    <td class="num">${f.model_prob != null ? (f.model_prob * 100).toFixed(0) + "% model" : "—"}</td>
    <td class="muted">${f.days_out != null ? `<span class="tag">${dateLabel(f.days_out)}</span> ` : ""}${esc(f.event)}</td></tr>`).join("");
  const favTbl = (p.favorite_ml || []).length
    ? `<label class="chalk-toggle"><input type="checkbox" id="hide-chalk" ${document.body.classList.contains("hide-chalk") ? "checked" : ""}> Hide obvious chalk (≥80%)</label><table><tbody>${favRows}</tbody></table>`
    : `<div class="empty">no clear favorites in the next few days</div>`;

  const ttLine = (p.team_total_over && p.team_total_over[0]) ? p.team_total_over[0].line : 1.5;
  const ttTbl = (p.team_total_over || []).length ? `<table><tbody>${p.team_total_over.map((t) => `<tr>
    <td><b>${esc(t.team)} Over ${t.line}</b></td>
    <td class="num">${(t.p_over * 100).toFixed(0)}% <span class="muted">model</span></td>
    <td class="num">${t.exp_goals != null ? t.exp_goals + " xG" : ""}</td>
    <td class="muted">${esc(t.event)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">none</div>`;

  return `<details class="browse" open><summary>${ico("markets")} Browse all candidates</summary><div class="browse-body">`
    + pickSection("Favorite moneylines", "clear favorites (≥55%)", favTbl)
    + pickSection("Team total — over " + ttLine, "model expected goals", ttTbl)
    + pickSection("Anytime goalscorer", "favorites' scorers first", propTable(p.anytime_goalscorer))
    + pickSection("Shots / shots on target — over", "by popularity", propTable(p.shots_sot))
    + pickSection("Popular props", "the Popular tab", propTable(p.popular_props))
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
      <div class="bh-label">Bankroll <span class="muted">· ${fmtMoney(s.unit_dollars || 10)}/unit</span></div>
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
    stat("Units P/L", o.units_pl ? sgn(o.units_pl) + "u" : "—", plCls) +
    stat("ROI", o.roi_pct == null ? "—" : o.roi_pct + "%", plCls);
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
      calBox.innerHTML = `<div class="cal-note">${ico("track")} Confidence calibration unlocks once a context bucket reaches 20 settled picks — favorite-ML picks now auto-settle from results, so this fills on its own. Best bucket so far: <b>${Math.round(best)}/20</b>.</div>`;
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
  const mc = d.summary.model_calibration || {};
  const mck = Object.keys(mc);
  const calTbl = mck.length ? `<div class="pick-section"><h3>Model calibration <span class="muted">· projected vs actual once picks settle · Brier lower = sharper (0.25 = coin flip)</span></h3>
    <table><thead><tr><th>Archetype</th><th class="num">n</th><th class="num">Model says</th><th class="num">Actual</th><th class="num">Gap</th><th class="num">Brier</th></tr></thead><tbody>${
    mck.map((k) => { const m = mc[k]; const over = m.gap_pp > 5, under = m.gap_pp < -5; return `<tr>
      <td><span class="tag">${esc(k.replace(/_/g, " "))}</span></td>
      <td class="num">${m.n}</td>
      <td class="num">${Math.round(m.mean_pred * 100)}%</td>
      <td class="num">${Math.round(m.hit_rate * 100)}%</td>
      <td class="num ${over ? "neg" : ""}">${sgn(m.gap_pp)}pp${over ? " over" : under ? " under" : ""}</td>
      <td class="num">${m.brier}</td></tr>`; }).join("")}</tbody></table>
    <div class="cal-note">${ico("track")} A large positive gap (model says &gt; actual) means the model over-projects that archetype. Populates from picks logged after this shipped.</div></div>` : "";
  $("#track-arch").innerHTML = archHtml + calTbl;

  // pick ledger — split Open / Settled, ordered by kickoff
  const body = $("#track-body");
  if (!d.picks.length) {
    body.innerHTML = `<div class="empty">${ico("track")}<div>No picks logged yet. Hit <b>Analyze</b> — every recommended bet auto-logs here and favorite MLs settle themselves.</div></div>`;
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
  const tab = groups[state.trackTab] ? state.trackTab : "open";
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
    await getJSON("/api/paper/" + sel.dataset.pstatus, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: sel.value }) });
    loadPaper();
  });
  body.querySelectorAll("[data-pmoney]").forEach((cb) => cb.onchange = async () => {
    await getJSON("/api/paper/" + cb.dataset.pmoney, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ real_money: cb.checked ? 1 : 0 }) });
    loadPaper();
  });
  body.querySelectorAll("[data-pdel]").forEach((b) => b.onclick = async () => {
    await getJSON("/api/paper/" + b.dataset.pdel, { method: "DELETE" });
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
function switchTab(name) {
  state.tab = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "track") loadPaper();
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
    const r = await getJSON("/api/propread", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
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
      const r = await getJSON("/api/futuresread", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
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
      await getJSON("/api/futures/lean", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ team: ln.dataset.team, kind: ln.dataset.kind, direction: ln.dataset.dir, entry_pct: parseFloat(ln.dataset.pct), note: note.trim() }) });
      await loadSnapshot();
    } catch (err) { ln.disabled = false; }
    return;
  }
  const rm = e.target.closest("[data-lean-rm]");
  if (rm) {
    rm.disabled = true;
    try { await getJSON("/api/futures/lean/" + encodeURIComponent(rm.dataset.leanRm), { method: "DELETE" }); await loadSnapshot(); }
    catch (err) { rm.disabled = false; }
    return;
  }
  const fv = e.target.closest("[data-fview]");
  if (fv) { localStorage.setItem("overlay_futures_view", fv.dataset.fview); renderFutures(); return; }
  const sc = e.target.closest("[data-scen-clear]");
  if (sc) { state.scenario = { pins: [], deltas: [] }; renderFutures(); return; }
  const pn = e.target.closest("[data-pin-w]");
  if (pn) { toggleScenarioPin(pn.dataset.pinA, pn.dataset.pinB, pn.dataset.pinW); return; }
});

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
  renderFutures();   // optimistic: show the pin immediately, fill deltas when the sim returns
  if (!state.scenario.pins.length) { state.scenario.deltas = []; renderFutures(); return; }
  try {
    const r = await getJSON("/api/futures/scenario", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pins: state.scenario.pins }) });
    state.scenario.deltas = r.deltas || [];
    state.scenario.pins = (r.pins && r.pins.length) ? r.pins : state.scenario.pins;   // backend drops pins it can't apply
    renderFutures();
  } catch (err) { /* keep the optimistic pin; deltas just stay empty */ }
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
  state.trackTab = c.dataset.track;
  if (state.paper) renderPaper();
});

// ---------- boot ----------
loadSnapshot();
// auto-refresh, but never yank the UI out from under an open read: skip the cycle while the user has
// a situational brief or a per-prop Read row expanded (they'll get fresh data on the next tick).
setInterval(() => {
  if (document.querySelector(".ai-research[open], tr.detail")) return;
  if (document.activeElement && document.activeElement.id === "scout-notes") return;  // don't yank notes mid-type
  loadSnapshot();
}, 60000);
