/* LedgerLoop console.
 *
 * Money is never computed here. Every amount arrives as {paise, inr} and only the
 * preformatted `inr` string is rendered -- the pipeline keeps amounts as integer paise
 * precisely so nothing rounds where nobody is looking, and doing arithmetic in a
 * language where 0.1 + 0.2 !== 0.3 would throw that away at the last step. If a number
 * is missing from the API, it is missing from the screen; it is never derived.
 *
 * That rule extends to motion. The ONLY figure that animates its digits is the
 * auto-match rate, which is a ratio the API sends as a float and which this file already
 * formats itself. Every rupee value animates as an opaque string -- its container may
 * fade or translate, but the characters are written once, exactly as received.
 *
 * No framework and no build step: the repo's setup story is `pip install -e .`, and a
 * node toolchain in front of the dashboard would break "clone and run" for a reviewer.
 * Animation is CSS transitions and keyframes driven by class and custom-property changes
 * from here; the only JS-driven tween in the file is the count-up, on rAF.
 */

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, props = {}, ...kids) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

/* Read live, not once at load: a reviewer can flip the OS setting mid-session, and a
   cached value would leave them with motion they just asked to stop. */
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

const STAGGER = 55; // ms between entrance steps; mirrors --stagger in styles.css

/* Bumped by hand whenever this file changes materially. Printed at startup so "is the
   browser running the code I just wrote?" is answerable in one glance instead of by
   inference from behaviour. */
const BUILD = "motion-1";

const state = {
  runId: null,
  summary: null,
  exceptions: null,
  tab: "queue",
  polling: null,
  reasonFilter: null,   // null = all reason codes
  expanded: new Set(),  // bank_line_ids whose evidence is open
  flash: null,          // {bankLineId, action} to acknowledge on the next queue render
  heroShown: null,      // last auto-match rate rendered, so a re-run counts from it
};

// -- api ------------------------------------------------------------------------------

async function api(path, options) {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch { /* a non-JSON error body is still an error; keep the status line */ }
    throw new Error(detail);
  }
  return response.json();
}

// -- motion helpers ---------------------------------------------------------------------

/** Stagger siblings by setting the --d custom property that the .enter class reads. */
function stagger(nodes, startMs = 0, step = STAGGER) {
  nodes.forEach((node, i) => {
    if (!node) return;
    node.classList.add("enter");
    node.style.setProperty("--d", `${startMs + i * step}ms`);
  });
}

/** Scroll-triggered reveal for section CONTAINERS. Never called on a table row: at
 *  20,000 postings, one observer entry per row is a real cost for no information. */
let revealObserver = null;
function revealOnScroll(nodes) {
  if (reduced()) {
    nodes.forEach((n) => n?.classList.add("reveal", "seen"));
    return;
  }
  revealObserver ??= new IntersectionObserver((entries, obs) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      entry.target.classList.add("seen");
      obs.unobserve(entry.target); // one-shot: nothing re-hides on scroll back up
    }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.05 });

  nodes.forEach((node) => {
    if (!node) return;
    node.classList.add("reveal");
    // Already on screen at render time (the common case for the first section): reveal
    // on the next frame rather than waiting for a scroll that may never come.
    requestAnimationFrame(() => {
      if (node.getBoundingClientRect().top < innerHeight * 0.95) node.classList.add("seen");
      else revealObserver.observe(node);
    });
  });
}

/** Count a PERCENTAGE up. Never used for money -- see the file header. The input is a
 *  float ratio from the API and the formatting is this file's own, so no string the
 *  server sent is ever taken apart to animate it. */
function countUp(node, from, to, ms = 700) {
  const format = (v) => `${(v * 100).toFixed(1)}%`;
  if (reduced() || ms <= 0 || Math.abs(to - from) < 0.0005) {
    node.textContent = format(to);
    return;
  }
  const started = performance.now();
  const tick = (now) => {
    const t = Math.min((now - started) / ms, 1);
    const eased = 1 - Math.pow(1 - t, 3); // easeOutCubic: decelerates onto the real figure
    node.textContent = format(from + (to - from) * eased);
    if (t < 1) requestAnimationFrame(tick);
    else node.textContent = format(to); // land exactly, never on a rounding artefact
  };
  requestAnimationFrame(tick);
}

// -- theme ----------------------------------------------------------------------------

function initTheme() {
  let stored = null;
  try { stored = localStorage.getItem("ll-theme"); } catch { /* private mode: fall back to OS */ }
  if (stored) document.documentElement.dataset.theme = stored;

  $("#theme-toggle").addEventListener("click", () => {
    const current = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("ll-theme", next); } catch { /* nothing to persist to */ }
  });
}

// -- tooltip --------------------------------------------------------------------------

const tooltip = {
  node: null,
  show(event, title, rows) {
    this.node ??= $("#tooltip");
    this.node.replaceChildren(
      el("div", { className: "t-title" }, title),
      ...rows.map((r) => el("div", { className: "t-row" }, r)),
    );
    this.move(event);
    // Position before the class flips, so the fade starts where it will land instead of
    // sliding across the screen from the previous segment's position.
    requestAnimationFrame(() => this.node.classList.add("on"));
  },
  move(event) {
    if (!this.node) return;
    const pad = 14;
    const box = this.node.getBoundingClientRect();
    let x = event.clientX + pad;
    let y = event.clientY + pad;
    if (x + box.width > innerWidth - 8) x = event.clientX - box.width - pad;
    if (y + box.height > innerHeight - 8) y = event.clientY - box.height - pad;
    this.node.style.left = `${Math.max(8, x)}px`;
    this.node.style.top = `${Math.max(8, y)}px`;
  },
  hide() { this.node?.classList.remove("on"); },
};

// -- icons ----------------------------------------------------------------------------

function svgIcon(d, size = 15, width = 2.4) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  node.setAttribute("width", String(size));
  node.setAttribute("height", String(size));
  node.setAttribute("viewBox", "0 0 24 24");
  node.setAttribute("fill", "none");
  node.setAttribute("stroke", "currentColor");
  node.setAttribute("stroke-width", String(width));
  node.setAttribute("stroke-linecap", "round");
  node.setAttribute("stroke-linejoin", "round");
  node.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  node.append(path);
  return node;
}

const icon = (kind) => svgIcon(kind === "pass" ? "M4 12.5l5.2 5.2L20 7" : "M6 6l12 12M18 6L6 18");

function verdict(ok, passText, failText) {
  return el("span", { className: `verdict ${ok ? "pass" : "fail"}` }, icon(ok ? "pass" : "fail"), ok ? passText : failText);
}

// -- bootstrap -------------------------------------------------------------------------

async function loadProfiles() {
  const profiles = await api("/profiles");
  const select = $("#profile");
  select.replaceChildren(
    ...profiles.map((p) => el("option", { value: p.name, textContent: `${p.name} — ${p.bank_lines} bank lines` })),
  );
  if (profiles.some((p) => p.name === "dev")) select.value = "dev";
  if (!profiles.length) {
    select.replaceChildren(el("option", { textContent: "no data generated" }));
    $("#run-btn").disabled = true;
  }
}

async function loadProviders() {
  try {
    const info = await api("/providers");
    const live = info.llm_available;
    setProviderDot(live ? "live" : "off");
    $("#provider-text").textContent = live
      ? `LLM: ${info.active}${info.pin ? " (pinned)" : ""}`
      : "LLM: none — deterministic";
    $("#provider-chip").title = live
      ? `Tier 3 chain: ${info.chain.join(" → ")}`
      : "No provider reachable. Tier 3 abstains and everything it would have adjudicated is escalated instead — a supported mode, not a failure.";
    if (info.default_reviewer && info.default_reviewer !== "unknown" && !$("#reviewer").value) {
      $("#reviewer").value = info.default_reviewer;
    }
  } catch {
    $("#provider-text").textContent = "LLM: unknown";
  }
}

/** `busy` is the only state that animates. A dot that always pulses is decoration and
 *  stops meaning anything; one that pulses while work is in flight is a status light. */
let providerKind = "off";
function setProviderDot(kind, busy = false) {
  if (kind) providerKind = kind;
  $("#provider-dot").className = `dot ${providerKind}${busy ? " busy" : ""}`;
}

// -- running ---------------------------------------------------------------------------

async function startRun() {
  const button = $("#run-btn");
  const status = $("#run-status");
  button.disabled = true;
  status.hidden = false;
  status.replaceChildren(el("span", { className: "spinner" }), " reconciling…");
  setProviderDot(null, true);

  try {
    const started = await api("/runs", {
      method: "POST",
      body: JSON.stringify({ profile: $("#profile").value, no_llm: $("#no-llm").checked }),
    });
    state.runId = started.run_id;
    await pollUntilDone();
  } catch (error) {
    status.replaceChildren(`failed: ${error.message}`);
    renderError(error.message);
  } finally {
    button.disabled = false;
    setProviderDot(null, false);
  }
}

function pollUntilDone() {
  return new Promise((resolve) => {
    const tick = async () => {
      let summary;
      try {
        summary = await api(`/runs/${state.runId}`);
      } catch (error) {
        renderError(error.message);
        return resolve();
      }
      if (summary.status === "running") {
        state.polling = setTimeout(tick, 600);
        return;
      }
      state.summary = summary;
      $("#run-status").hidden = true;
      if (summary.status === "failed") {
        renderError(summary.error || "run failed");
        return resolve();
      }
      state.exceptions = await api(`/runs/${state.runId}/exceptions`);
      render();
      announce(
        `Run complete. ${summary.resolved_count} of ${summary.total_records} resolved. `
        + `${summary.exceptions_needing_review} need review.`,
      );
      resolve();
    };
    tick();
  });
}

function announce(message) { $("#live").textContent = message; }

function renderError(message) {
  const card = el("div", { className: "card empty" },
    el("h2", {}, "That run didn't complete"),
    el("p", {}, message),
  );
  stagger([card]);
  $("#stage").replaceChildren(card);
}

// -- render ------------------------------------------------------------------------------

function render() {
  const s = state.summary;
  if (!s || s.status !== "complete") return;

  const summary = summarySection(s);
  const viz = dispositionSection(s);
  const tabs = tabStrip(s);
  const panel = el("div", { id: "panel" });

  // Continues the stagger the masthead (0ms) and control bar (60ms) started in the HTML.
  // The panel is not staggered -- it runs its own panel-enter once its data arrives,
  // which is later anyway.
  stagger([summary, viz, tabs], 120);

  $("#stage").replaceChildren(summary, viz, tabs, panel);
  positionUnderline(tabs);
  renderPanel();
}

function pct(value) { return `${(value * 100).toFixed(1)}%`; }

function summarySection(s) {
  const tiers = s.tier_counts || {};
  const deterministic = (tiers.tier1 || 0) + (tiers.tier2 || 0);
  const escalatedValue = state.exceptions?.escalated_value?.inr ?? "—";

  // The one animated figure in the app: a ratio, formatted here. A re-run counts from
  // the previous rate rather than from zero, so what moves is the delta.
  const heroValue = el("div", { className: "value" }, pct(state.heroShown ?? 0));
  const from = state.heroShown ?? 0;
  state.heroShown = s.auto_match_rate;
  requestAnimationFrame(() => countUp(heroValue, from, s.auto_match_rate));

  return el("section", { className: "summary" },
    el("div", { className: "card hero" },
      el("div", { className: "label" }, "Auto-matched"),
      heroValue,
      el("div", { className: "sub" },
        `${s.resolved_count} of ${s.total_records} bank lines, no human touched them`),
    ),
    el("div", { className: "kpis" },
      kpi("Needs a decision", String(s.exceptions_needing_review),
        `${s.exceptions_no_action} more need no action`),
      kpi("Escalated value", escalatedValue, "sitting in the review queue"),
      kpi("LLM calls", String(s.llm_calls_made),
        s.providers_used?.length ? s.providers_used.join(", ") : "none — deterministic only"),
      s.candidates_suppressed_by_review
        ? kpi("Review feedback", String(s.candidates_suppressed_by_review),
            `pairing${s.candidates_suppressed_by_review === 1 ? "" : "s"} a reviewer rejected, not re-proposed`)
        : kpi("Deterministic", String(deterministic), "settled before any model ran"),
    ),
  );
}

function kpi(label, value, sub) {
  return el("div", { className: "card kpi" },
    el("div", { className: "label" }, label),
    el("div", { className: "value" }, value),  // money arrives preformatted; written once
    el("div", { className: "sub" }, sub),
  );
}

/* The disposition bar is part-to-whole over one batch, so: a single stacked bar.
 * The tiers take an ordinal ramp (one hue, light to dark) because they are ordered
 * stages of one process, not four identities -- and escalated takes neutral gray
 * because it is not a fourth stage. Four segments, so every one is direct-labelled in
 * the legend as well as coloured; identity never rests on colour alone.
 *
 * Each segment holds its final width from the first frame and an inner fill scales in
 * from 0. Growing the segment's own width would relayout the flex row on every frame;
 * scaleX composites and produces the same picture. */
function dispositionSection(s) {
  const t = s.tier_counts || {};
  const segments = [
    { key: "tier1", label: "Tier 1 — exact join", n: t.tier1 || 0, note: "UTR and amount matched outright" },
    { key: "tier2", label: "Tier 2 — algorithmic", n: t.tier2 || 0, note: "fee model, then subset-sum search" },
    { key: "tier3", label: "Tier 3 — LLM adjudication", n: t.tier3 || 0, note: "picked from a fixed candidate menu" },
    { key: "escalated", label: "Escalated", n: s.exception_count || 0, note: "typed, explained, queued for a human" },
  ];
  const total = segments.reduce((sum, seg) => sum + seg.n, 0) || 1;

  const stack = el("div", { className: "stack", role: "img",
    "aria-label": segments.map((seg) => `${seg.label}: ${seg.n}`).join("; ") });

  let shown = 0;
  for (const seg of segments) {
    if (!seg.n) continue;
    const fill = el("div", { className: "seg-fill" });
    // Left to right, in the order a line would actually travel through the tiers.
    fill.style.setProperty("--d", `${260 + shown * 90}ms`);
    shown += 1;

    const node = el("div", { className: `seg seg-${seg.key}` }, fill);
    node.style.flexGrow = String(seg.n);
    node.style.flexBasis = "0";
    node.addEventListener("pointerenter", (e) => tooltip.show(e, seg.label, [
      `${seg.n} of ${total} lines — ${pct(seg.n / total)}`, seg.note,
    ]));
    node.addEventListener("pointermove", (e) => tooltip.move(e));
    node.addEventListener("pointerleave", () => tooltip.hide());
    stack.append(node);
  }

  const residual = segments[2].n + segments[3].n;
  return el("section", { className: "card viz" },
    el("div", { className: "viz-head" },
      el("h2", {}, "How every bank line was disposed of"),
      el("span", { className: "chip" }, `${total} lines`),
    ),
    el("p", { className: "viz-note" },
      "Cheapest and most certain first. Each tier only ever sees what the one before it "
      + `could not settle — the LLM was handed ${residual} line${residual === 1 ? "" : "s"}, `
      + "never the whole batch, and can only choose from candidates tier 2 already computed."),
    stack,
    el("div", { className: "legend" },
      ...segments.map((seg) => el("div", { className: "legend-item" },
        Object.assign(el("span", { className: "swatch" }), {
          style: `background: var(--${seg.key === "escalated" ? "escalated" : seg.key})`,
        }),
        seg.label, " ", el("b", {}, String(seg.n)),
      )),
    ),
  );
}

function tabStrip(s) {
  const tabs = [
    ["queue", "Exception queue", s.exceptions_needing_review],
    ["journal", "Journal entries", s.postings_total],
    ["tieout", "Tie-out", null],
    ["audit", "Audit trail", null],
  ];
  const strip = el("div", { className: "tabs", role: "tablist" });
  for (const [key, label, count] of tabs) {
    const button = el("button", { className: "tab", role: "tab", type: "button" },
      label, count != null ? el("span", { className: "count" }, ` ${count}`) : null);
    button.setAttribute("aria-selected", String(state.tab === key));
    button.addEventListener("click", () => {
      if (state.tab === key) return;
      state.tab = key;
      // Only the tab strip and the panel change. Re-rendering the whole page here reset
      // the reader's scroll position and rebuilt the chart on every tab click.
      for (const other of strip.querySelectorAll(".tab")) {
        other.setAttribute("aria-selected", String(other === button));
      }
      positionUnderline(strip);
      renderPanel();
    });
    strip.append(button);
  }
  strip.append(el("div", { className: "tab-underline" }));
  return strip;
}

/** Move the single underline to the active tab. Transform only, so the slide costs
 *  nothing -- and per-tab bottom borders could not slide at all, only appear and
 *  disappear, which loses the relation between the tab you left and the one you reached. */
function positionUnderline(strip) {
  const bar = strip.querySelector(".tab-underline");
  const active = strip.querySelector('.tab[aria-selected="true"]');
  if (!bar || !active) return;
  const move = () => {
    bar.style.setProperty("--x", `${active.offsetLeft}px`);
    bar.style.setProperty("--w", String(active.offsetWidth));
  };
  if (bar.dataset.placed) {
    move();
    return;
  }
  // First placement must not slide in from x=0; suppress the transition for one frame.
  bar.style.transition = "none";
  requestAnimationFrame(() => {
    move();
    bar.dataset.placed = "1";
    requestAnimationFrame(() => { bar.style.transition = ""; });
  });
}

function renderPanel() {
  const panel = $("#panel");
  if (!panel) return;
  panel.replaceChildren(el("div", { className: "empty" }, el("span", { className: "spinner" })));
  const renderers = { queue: renderQueue, journal: renderJournal, tieout: renderTieOut, audit: renderAudit };
  renderers[state.tab](panel)
    .then(() => {
      // Restart the crossfade, so returning to a tab animates rather than reusing a
      // finished animation. One reflow, on a container -- never inside a table loop.
      panel.classList.remove("panel-enter");
      void panel.offsetWidth;
      panel.classList.add("panel-enter");
    })
    .catch((error) => {
      panel.replaceChildren(el("div", { className: "card empty" }, el("p", {}, error.message)));
    });
}

// -- queue --------------------------------------------------------------------------------

async function renderQueue(panel) {
  state.exceptions ??= await api(`/runs/${state.runId}/exceptions`);
  const { needs_review: allNeeds, no_action: allNone, reason_codes: codes } = state.exceptions;

  const match = (item) => !state.reasonFilter || item.reason_code === state.reasonFilter;
  const needs = allNeeds.filter(match);
  const none = allNone.filter(match);

  const children = [filterBar(codes, allNeeds, allNone)];

  children.push(el("div", { className: "section-head" },
    el("h2", {}, `Needs a decision (${needs.length})`),
    el("p", {}, "each carries its own reasoning, computed by the pipeline"),
  ));
  children.push(needs.length
    ? needs.map(excCard)
    : el("div", { className: "card empty" }, el("p", {},
        state.reasonFilter ? "No open items with that reason code."
                           : "Nothing here needs a human. Every line was disposed of.")));

  if (none.length) {
    children.push(el("div", { className: "section-head" },
      el("h2", {}, `No action required (${none.length})`),
      el("p", {}, "bank credits that were never gateway settlements — declining to match them is the right answer, already taken"),
    ));
    children.push(none.map(excCard));
  }
  panel.replaceChildren(...children.flat());
  flashDecidedCard(panel);
}

/** Acknowledge the card that was just decided, once it is back in the DOM. A decided
 *  card STAYS in the list -- the queue does not hide what a reviewer has touched -- so
 *  this reads as "recorded", not as an exit. */
function flashDecidedCard(panel) {
  if (!state.flash) return;
  const { bankLineId, action } = state.flash;
  state.flash = null;
  if (reduced()) return;
  const card = panel.querySelector(`[data-line="${CSS.escape(bankLineId)}"]`);
  if (!card) return;
  card.classList.add("flash", `flash-${action}`);
  card.addEventListener("animationend", () => card.classList.remove("flash", `flash-${action}`), { once: true });
}

/* Filters sit in one row above the list, and every chip carries its own count so the
   reader can see the shape of the queue before touching anything. */
function filterBar(codes, needs, none) {
  const all = [...needs, ...none];
  const bar = el("div", { className: "filterbar" });

  const chip = (label, value, count) => {
    const active = state.reasonFilter === value;
    const button = el("button", { className: `filter${active ? " on" : ""}`, type: "button" },
      label, el("span", { className: "filter-count" }, String(count)));
    button.setAttribute("aria-pressed", String(active));
    button.addEventListener("click", () => {
      state.reasonFilter = active ? null : value;
      renderPanel();
    });
    return button;
  };

  bar.append(chip("All", null, all.length));
  for (const code of codes) {
    bar.append(chip(code, code, all.filter((i) => i.reason_code === code).length));
  }
  return bar;
}

function excCard(item) {
  const card = el("div", { className: "card exc" });
  card.dataset.line = item.bank_line_id;

  card.append(el("div", { className: "exc-head" },
    el("span", { className: "exc-id" }, item.bank_line_id),
    el("span", { className: "code" }, item.reason_code),
    el("span", { className: `status-pill status-${item.status}` }, item.status),
    // Preformatted rupee string, written verbatim. Never counted up, never split.
    item.credit ? el("span", { className: "exc-amount" }, item.credit.inr) : null,
  ));

  card.append(el("p", { className: "exc-explain" },
    item.explanation || el("em", {}, "No explanation recorded for this line.")));

  if (item.reviewer_note) {
    card.append(el("p", { className: "hint" }, `Note: ${item.reviewer_note}`));
  }

  if (item.candidates?.length) card.append(evidenceBlock(item));

  if (item.requires_review) {
    const note = el("input", { type: "text", placeholder: "note (optional)", value: item.reviewer_note || "" });
    const actions = el("div", { className: "exc-actions" }, note);
    for (const action of ["approved", "rejected", "reassigned"]) {
      const button = el("button", { className: "btn btn-sm", type: "button" }, action.replace(/ed$/, ""));
      button.addEventListener("click", () => decide(item.bank_line_id, action, note.value, card));
      actions.append(button);
    }
    card.append(actions);
  }
  return card;
}

/* The explanation says "groups 4 transactions"; this says WHICH four.
 *
 * A reviewer cannot judge a proposed match from a count and an opaque handle like
 * BANK00115-C0 -- they need the transaction ids, the rule that produced the grouping and
 * any residual difference. Collapsed by default so the queue still reads as a queue, and
 * the open/closed state is kept per bank line across re-renders so recording a decision
 * doesn't snap everything shut.
 *
 * The body is always in the DOM and opens via a 0fr -> 1fr grid track, so nothing is
 * measured and there is no max-height to guess wrong when a candidate list wraps. */
function evidenceBlock(item) {
  const open = state.expanded.has(item.bank_line_id);
  const wrap = el("div", { className: `evidence${open ? " open" : ""}` });
  const plural = item.candidates.length === 1 ? "" : "s";
  const label = (isOpen) => `${isOpen ? "Hide" : "Show"} the ${item.candidates.length} candidate${plural} considered`;

  const caret = svgIcon("M9 5l7 7-7 7", 12, 2.6);
  caret.classList.add("caret");
  const text = el("span", {}, label(open));
  const toggle = el("button", { className: "evidence-toggle", type: "button" }, caret, text);
  toggle.setAttribute("aria-expanded", String(open));
  toggle.addEventListener("click", () => {
    const nowOpen = !wrap.classList.contains("open");
    if (nowOpen) state.expanded.add(item.bank_line_id);
    else state.expanded.delete(item.bank_line_id);
    // Toggled in place: re-rendering the panel would replace the node mid-transition and
    // the height animation would never run.
    wrap.classList.toggle("open", nowOpen);
    toggle.setAttribute("aria-expanded", String(nowOpen));
    text.textContent = label(nowOpen);
  });
  wrap.append(toggle);

  const inner = el("div", { className: "evidence-inner" });
  if (item.value_date || item.extracted_utr) {
    inner.append(el("p", { className: "hint", style: "margin:8px 0 0" },
      [item.value_date ? `Value date ${item.value_date}` : null,
       item.extracted_utr ? `UTR ${item.extracted_utr}` : "no UTR extracted"]
        .filter(Boolean).join(" · ")));
  }
  for (const c of item.candidates) {
    const head = el("div", { className: "cand-head" },
      el("span", { className: "mono" }, c.candidate_id || "candidate"),
      c.rule ? el("span", { className: "code" }, c.rule) : null,
      c.score != null ? el("span", { className: "hint", style: "margin:0" }, `score ${Number(c.score).toFixed(2)}`) : null,
      // Preformatted money string again -- rendered as received.
      c.amount_diff && c.amount_diff.paise
        ? el("span", { className: "hint", style: "margin:0" }, `Δ ${c.amount_diff.inr}`)
        : null,
      c.rejected_by_reviewer
        ? el("span", { className: "status-pill status-rejected" }, "you rejected this — not re-proposed")
        : null,
    );
    const body = el("div", { className: "cand-txns mono" },
      (c.matched_txn_ids || []).join("  ") || "—");
    inner.append(el("div", { className: `cand${c.rejected_by_reviewer ? " cand-rejected" : ""}` }, head, body));
  }
  wrap.append(el("div", { className: "evidence-body" }, inner));
  return wrap;
}

async function decide(bankLineId, action, note, card) {
  card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    const result = await api(`/runs/${state.runId}/decisions`, {
      method: "POST",
      body: JSON.stringify({
        bank_line_id: bankLineId,
        action,
        actor: $("#reviewer").value.trim() || null,
        note: note.trim() || null,
      }),
    });
    state.exceptions = result;
    state.flash = { bankLineId, action };
    announce(result.was_new
      ? `${bankLineId} ${action}.`
      : `${bankLineId} was already ${action} — recorded once, not twice.`);
    renderPanel();
  } catch (error) {
    card.append(el("p", { className: "hint" }, `Could not record that: ${error.message}`));
    card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
  }
}

// -- journal -------------------------------------------------------------------------------

async function renderJournal(panel) {
  const data = await api(`/runs/${state.runId}/journal`);
  const children = [];

  children.push(el("div", { className: `banner ${data.all_balanced ? "good" : "bad"}` },
    verdict(data.all_balanced, "Every batch balances", "A batch does not balance"),
    el("span", { className: "hint", style: "margin:0" },
      `${data.posting_count.toLocaleString()} postings across ${data.batches.length.toLocaleString()} batches — `
      + `${data.new_posting_count.toLocaleString()} not yet approved`),
  ));

  const approveBtn = el("button", { className: "btn btn-primary", type: "button" },
    "Approve postings, then re-run");
  const outcome = el("span", { className: "hint", style: "margin:0" });
  approveBtn.addEventListener("click", () => approveAndRerun(approveBtn, outcome));

  children.push(el("div", { className: "card controls", style: "margin-bottom:18px" },
    el("div", { style: "flex:1;min-width:240px" },
      el("h3", {}, "Close the loop"),
      el("p", { className: "hint", style: "margin-top:4px" },
        "Approve these postings, then run the identical batch again. The second run "
        + "proposes zero new postings — the reconciliation closed rather than double-posting."),
    ),
    approveBtn, outcome,
  ));

  // One row per posting would be 20k rows; the batch is the unit a reviewer works in.
  // Rows carry no animation of any kind -- see the tables note in styles.css.
  const rows = data.batches.slice(0, 300).map((b) => el("tr", {},
    el("td", { className: "mono" }, b.bank_line_id),
    el("td", {}, b.resolved_by),
    el("td", { className: "mono" }, b.settlement_batch_ids.join(", ") || "—"),
    el("td", { className: "num" }, String(b.postings.length)),
    el("td", { className: "num" }, b.debits.inr),
    el("td", { className: "num" }, b.credits.inr),
    el("td", {}, verdict(b.balanced, "balanced", "off")),
  ));

  const table = el("div", { className: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Bank line"), el("th", {}, "Resolved by"), el("th", {}, "Settlement batch"),
        el("th", { className: "num" }, "Postings"), el("th", { className: "num" }, "Debits"),
        el("th", { className: "num" }, "Credits"), el("th", {}, "Control"))),
      el("tbody", {}, ...rows),
    ),
  );
  children.push(table);
  if (data.batches.length > rows.length) {
    children.push(el("p", { className: "hint" },
      `Showing the first ${rows.length} of ${data.batches.length.toLocaleString()} batches.`));
  }
  panel.replaceChildren(...children);
  revealOnScroll([table]); // the container, once -- not its 300 rows
}

async function approveAndRerun(button, outcome) {
  button.disabled = true;
  outcome.replaceChildren(el("span", { className: "spinner" }), " approving…");
  setProviderDot(null, true);
  try {
    const first = await api(`/runs/${state.runId}/approve`, { method: "POST" });
    outcome.replaceChildren(el("span", { className: "spinner" }), ` ${first.new_postings} newly approved — re-running…`);

    const rerun = await api("/runs", {
      method: "POST",
      body: JSON.stringify({ profile: state.summary.profile, no_llm: state.summary.no_llm }),
    });
    state.runId = rerun.run_id;
    state.exceptions = null;
    await pollUntilDone();

    const second = state.summary.postings_new;
    const panel = $("#panel");
    if (panel) {
      const banner = el("div", { className: `banner ${second === 0 ? "good" : "bad"}` },
        verdict(second === 0, "Zero new postings on the re-run", `${second} new postings on the re-run`),
        el("span", { className: "hint", style: "margin:0" },
          second === 0
            ? `${first.new_postings} approved, then the identical batch proposed nothing further. The loop is closed.`
            : "Expected zero — the same batch should not post twice."),
      );
      stagger([banner]);
      panel.prepend(banner);
    }
    announce(second === 0 ? "Re-run produced zero new postings." : `Re-run produced ${second} new postings.`);
  } catch (error) {
    outcome.replaceChildren(`failed: ${error.message}`);
  } finally {
    button.disabled = false;
    setProviderDot(null, false);
  }
}

// -- tie-out --------------------------------------------------------------------------------

async function renderTieOut(panel) {
  const t = await api(`/runs/${state.runId}/tieout`);
  const c = t.controls;

  const stmtRow = (label, amount, extra, isTotal) => el("div", { className: `stmt-row${isTotal ? " total" : ""}` },
    el("span", { className: "lede" }, label),
    extra ? el("span", { className: "hint", style: "margin:0" }, extra) : null,
    el("span", { className: "dots" }),
    el("span", { className: "amt" }, amount),  // preformatted, verbatim
  );

  const statement = el("section", { className: "card statement" },
    el("h2", { style: "margin-bottom:10px" }, "Reconciliation statement"),
    stmtRow("Bank statement", t.statement.total.inr, `${t.statement.line_count} credits`, true),
    stmtRow("Reconciled", t.statement.reconciled.inr, `${t.statement.reconciled_line_count} lines`),
    stmtRow("Unreconciled", t.statement.unreconciled.inr, `${t.statement.unreconciled_line_count} lines`),
  );

  const duplicates = Object.keys(c.duplicate_receivable_relief).length;
  const grid = el("div", { className: "controls-grid" },
    controlCard("Cash ties out", c.cash_ties_out,
      `Bank receipts posted ${c.bank_receipt_total.inr} against ${t.statement.reconciled.inr} reconciled.`),
    controlCard("Books balance", c.balances,
      `Debits ${c.total_debits.inr} against credits ${c.total_credits.inr}, across the whole run — not just per batch.`),
    controlCard("No receivable cleared twice", duplicates === 0,
      duplicates === 0
        ? "No transaction had its receivable relieved by more than one bank line."
        : `${duplicates} transactions were cleared by two different bank lines.`),
    el("div", { className: "card control-card" },
      el("span", { className: "name" }, "Fee drift absorbed"),
      el("span", { className: "verdict warn" }, c.rounding_adjustment_gross.inr),
      el("span", { className: "detail" },
        `${c.rounding_adjustment_count} rounding postings, net ${c.rounding_adjustment_net.inr}. `
        + "Reported, not hidden — the tolerance absorbed it."),
    ),
  );

  const movements = el("div", { className: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Account"), el("th", { className: "num" }, "Debit"),
        el("th", { className: "num" }, "Credit"), el("th", { className: "num" }, "Postings"))),
      el("tbody", {}, ...t.movements.map((m) => el("tr", {},
        el("td", { className: "mono" }, m.account),
        el("td", { className: "num" }, m.debit.inr),
        el("td", { className: "num" }, m.credit.inr),
        el("td", { className: "num" }, m.posting_count.toLocaleString()),
      ))),
    ),
  );

  panel.replaceChildren(
    el("div", { className: `banner ${t.clean ? "good" : "bad"}` },
      verdict(t.clean, "Tie-out clean", "Tie-out has a finding"),
      el("span", { className: "hint", style: "margin:0" },
        t.clean
          ? "cash and books agree, and no transaction was relieved twice"
          : "at least one control did not pass — see below"),
    ),
    statement,
    grid,
    el("h3", { style: "margin:22px 0 10px" }, "Movement by control account"),
    movements,
  );

  // Three containers, revealed as they come into view. This is the longest panel, so it
  // is the one where scrolling into a section is a real event rather than a gimmick.
  revealOnScroll([statement, grid, movements]);
}

function controlCard(name, ok, detail) {
  return el("div", { className: "card control-card" },
    el("span", { className: "name" }, name),
    verdict(ok, "Pass", "Finding"),
    el("span", { className: "detail" }, detail),
  );
}

// -- audit -----------------------------------------------------------------------------------

async function renderAudit(panel) {
  const data = await api(`/runs/${state.runId}/audit?limit=200`);
  if (!data.entries.length) {
    panel.replaceChildren(el("div", { className: "card empty" }, el("p", {}, "No audit entries yet.")));
    return;
  }
  const rows = data.entries.map((e) => el("tr", {},
    el("td", { className: "mono" }, e.timestamp_utc || "—"),
    el("td", { className: "mono" }, e.bank_line_id || "—"),
    el("td", {}, e.resolved_by || e.review_action || "—"),
    el("td", { className: "mono" }, e.rule || e.reason_code || "—"),
    el("td", { className: "num" }, e.confidence != null ? Number(e.confidence).toFixed(2) : "—"),
    el("td", {}, e.review_actor || "—"),
  ));
  const table = el("div", { className: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Timestamp (UTC)"), el("th", {}, "Bank line"), el("th", {}, "Decision"),
        el("th", {}, "Rule / reason"), el("th", { className: "num" }, "Confidence"), el("th", {}, "Actor"))),
      el("tbody", {}, ...rows),
    ),
  );
  panel.replaceChildren(
    el("p", { className: "hint", style: "margin:0 0 10px" },
      `Most recent ${data.entries.length} of ${data.total.toLocaleString()} entries. Append-only — never rewritten.`),
    table,
  );
  revealOnScroll([table]); // container only
}

// -- go ------------------------------------------------------------------------------------------

/* A one-line diagnostic, on purpose. "The UI looks unchanged" has two very different
   causes -- the browser is running an older file, or the OS has asked for reduced motion
   and every animation is correctly switched off -- and from the outside they are
   identical. Guessing between them costs more than printing the answer. */
console.log(
  `[LedgerLoop] console ${BUILD} | reduced-motion: ${reduced() ? "ON (animation suppressed by OS setting)" : "off"}`,
);

initTheme();
$("#run-btn").addEventListener("click", startRun);
loadProfiles();
loadProviders();
