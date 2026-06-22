"use strict";

const state = { snapshot: null, paper: null, tab: "picks", propIndex: {}, dayFilter: null, trackTab: "open" };

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
  renderResearch();
  if (state.tab === "track") loadPaper();
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
  $("#track-arch").innerHTML = akeys.length ? `<div class="pick-section"><h3>By archetype</h3><table><thead><tr>
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
  loadSnapshot();
}, 60000);
