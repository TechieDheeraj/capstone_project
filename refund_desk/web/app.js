/* Refund Desk UI: pick a ticket, run the agent, watch every step stream in, approve, inspect. */
(function () {
  "use strict";
  var $ = function (s) { return document.querySelector(s); };
  var state = { health: null, tickets: [], current: null, runId: null, seen: 0, es: null, step: null };

  function h(tag, attrs, kids) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) n.setAttribute(k, attrs[k]);
    });
    (Array.isArray(kids) ? kids : kids === undefined ? [] : [kids]).forEach(function (c) {
      if (c === null || c === undefined || c === false) return;
      n.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    });
    return n;
  }
  function api(path, body) {
    var opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    return fetch(path, opt).then(function (r) {
      if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.detail || ("HTTP " + r.status)); });
      return r.json();
    });
  }
  function money(c) { return "$" + (c / 100).toFixed(2); }
  function usd(x) { return "$" + Number(x || 0).toFixed(4); }
  function short(o) { var s = typeof o === "string" ? o : JSON.stringify(o); return s.length > 140 ? s.slice(0, 137) + "…" : s; }

  /* ---------- theme (per-viewer convenience) ---------- */
  function setTheme(t) { if (t) document.documentElement.setAttribute("data-theme", t); else document.documentElement.removeAttribute("data-theme"); }
  try { setTheme(localStorage.getItem("rd-theme")); } catch (e) { /* storage blocked */ }
  $("#theme").addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme");
    var dark = cur ? cur === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    var next = dark ? "light" : "dark";
    setTheme(next);
    try { localStorage.setItem("rd-theme", next); } catch (e) { /* ignore */ }
  });

  /* ---------- tabs ---------- */
  document.querySelectorAll(".tabs button").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".tabs button").forEach(function (x) { x.setAttribute("aria-selected", String(x === b)); });
      ["seen", "trace", "spend"].forEach(function (t) { $("#tab-" + t).hidden = t !== b.dataset.tab; });
    });
  });

  /* ---------- inbox ---------- */
  function renderTickets() {
    var ul = $("#tickets");
    ul.textContent = "";
    state.tickets.forEach(function (t) {
      ul.appendChild(h("li", {}, h("button", { type: "button", "aria-current": String(state.current && state.current.id === t.id), onclick: function () { pick(t); } }, [
        h("span", { class: "id", text: t.id }), h("span", { class: "s", text: t.subject }), h("span", { class: "f", text: t.from_name })
      ])));
    });
  }
  function pick(t) {
    state.current = t;
    renderTickets();
    var card = $("#ticket-card");
    card.textContent = "";
    card.appendChild(h("div", { class: "meta" }, [h("span", { text: t.id }), h("span", { text: "From: " + t.from_name + " <" + t.from_email + ">" })]));
    card.appendChild(h("h2", { text: t.subject }));
    card.appendChild(h("div", { class: "body", text: t.body }));
    card.appendChild(h("div", { class: "actions" }, [
      h("button", { class: "primary", type: "button", id: "run-btn", onclick: function () { run(t); } }, "Run the agent"),
      h("span", { class: "hint", text: "Every refund and every e-mail waits for your approval." })
    ]));
  }
  function loadRuns() {
    api("/api/runs").then(function (rows) {
      var ul = $("#runs");
      ul.textContent = "";
      rows.slice(0, 12).forEach(function (r) {
        ul.appendChild(h("li", { title: r.id, onclick: function () { openRun(r.id); } }, [
          h("span", { text: r.ticket + " · " + (r.action || r.status) }), h("span", { class: "muted", text: usd(r.cost_usd) })
        ]));
      });
    }).catch(function () { /* the list is optional */ });
  }

  $("#compose").addEventListener("submit", function (e) {
    e.preventDefault();
    var f = new FormData(e.target);
    var t = { id: "W-" + Date.now().toString(36).slice(-5).toUpperCase(), from_name: f.get("from_name"), from_email: f.get("from_email"),
      subject: f.get("subject"), body: f.get("body") };
    pick(t);
    run(t, true);
  });
  $("#carrier-down").addEventListener("change", function (e) {
    api("/api/demo/carrier?on=" + e.target.checked, {}).catch(function (err) { alertBox("Could not switch the fake carrier: " + err.message); e.target.checked = !e.target.checked; });
  });

  /* ---------- a run ---------- */
  function reset() {
    $("#timeline").textContent = "";
    $("#approval").textContent = "";
    $("#reply").textContent = "";
    $("#reply-wrap").hidden = true;
    $("#done").hidden = true;
    $("#done").textContent = "";
    $("#run-head").hidden = true;
    state.step = null;
    state.seen = 0;
    if (state.es) { state.es.close(); state.es = null; }
  }
  function run(t, custom) {
    reset();
    var btn = $("#run-btn");
    if (btn) btn.disabled = true;
    var body = custom || !t.id.match(/^T-\d+$/) ? { ticket: t } : { ticket_id: t.id };
    api("/api/runs", body).then(function (r) { state.runId = r.run_id; listen(); })
      .catch(function (err) { alertBox(err.message); if (btn) btn.disabled = false; });
  }
  function listen() {
    var es = new EventSource("/api/runs/" + state.runId + "/events?start=" + state.seen);
    state.es = es;
    es.onmessage = function (m) { state.seen++; onEvent(JSON.parse(m.data)); };
    es.onerror = function () { es.close(); };
  }
  function alertBox(msg) { $("#timeline").appendChild(h("li", { class: "warn" }, [h("span", { class: "t", text: msg })])); }

  function item(cls, kind, title, detail) {
    var li = h("li", { class: cls }, [
      h("div", { class: "row" }, [h("span", { class: "t", text: title }), h("span", { class: "k", text: kind })]),
      detail ? h("div", { class: "d", text: detail }) : null
    ]);
    $("#timeline").appendChild(li);
    li.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return li;
  }

  var DECISIONS = {
    pii_redacted: function (a) { return ["ok", "Personal data replaced by placeholders", Object.keys(a.counts || {}).map(function (k) { return k + " × " + a.counts[k]; }).join(", ")]; },
    injection_suspected: function (a) { return ["warn", "Possible prompt injection in " + a.source, "rules: " + a.rule + " · the text stays data; a human sees this flag"]; },
    approval_requested: function (a) { return ["ok", "Paused for a human: " + a.kind, a.amount_cents ? money(a.amount_cents) : ""]; },
    approval_decided: function (a) { return [a.approved ? "ok" : "warn", (a.approved ? "Approved: " : "Rejected: ") + a.kind, (a.by || "") + (a.reason ? " · " + a.reason : "")]; },
    budget_stop: function (a) { return ["warn", "Budget limit reached: " + a.limit, a.used + " of " + a.cap + " · handed to a human"]; },
    fallback_used: function (a) { return ["warn", "Fallback model used", a.provider + " · " + a.model]; },
    provider_down: function (a) { return ["warn", "Provider down: " + a.provider, a.error]; },
    guard_blocked: function (a) { return ["warn", "Blocked by a code guard", a.reason]; },
    finish_rejected: function (a) { return ["warn", "Final answer rejected by the harness", a.reason]; },
    loop_guard: function (a) { return ["warn", "Loop guard: stop calling " + a.tool, ""]; },
    permission_denied: function (a) { return ["warn", "Permission denied: " + a.tool, "missing " + (a.missing || []).join(", ")]; },
    email_sent: function (a) { return ["ok", "E-mail placed in the outbox", a.email_id + (a.duplicate ? " (duplicate ignored)" : "")]; },
    resumed: function (a) { return ["info", "Resumed from the checkpoint", "step " + a.step]; },
    finish: function (a) { return ["info", "Final decision: " + a.action, a.refund_cents ? money(a.refund_cents) : (a.refs || []).join(", ")]; }
  };

  function onEvent(ev) {
    var a = ev.attrs || {};
    switch (ev.type) {
      case "run_started":
        var head = $("#run-head");
        head.hidden = false;
        head.textContent = "";
        head.appendChild(h("span", { class: "chip " + (a.route === "large" ? "deep" : "llm"), text: a.route + " route" }));
        head.appendChild(h("span", { class: "chip llm", text: a.model }));
        head.appendChild(h("span", { class: "chip", text: "prompt " + a.prompt }));
        if (a.arm && a.arm !== "none") head.appendChild(h("span", { class: "chip", text: "A/B: " + a.arm }));
        head.appendChild(h("span", { class: "reasons", text: "why: " + (a.reasons || []).join(", ") }));
        break;
      case "model_start":
        state.step = item("model", "model call", "The model thinks", null);
        state.step.querySelector(".t").classList.add("spin");
        break;
      case "model_end":
        if (state.step) {
          state.step.querySelector(".t").classList.remove("spin");
          state.step.querySelector(".t").textContent = a.cache === "hit" ? "Cache hit: no model call" : "The model answered";
          state.step.appendChild(h("div", { class: "d", text: a.cache === "hit" ? "$0 · same request as before"
            : (a.model || "") + " · " + (a.tokens_in || 0) + " in / " + (a.tokens_out || 0) + " out tokens · " + usd(a.cost_usd) + (a.simulated ? " (simulated)" : "") + (a.retries ? " · retries " + a.retries : "") }));
        }
        break;
      case "delta":
        if (ev.kind === "reply") {
          $("#reply-wrap").hidden = false;
          $("#reply").textContent += ev.text;
        } else if (state.step) {
          var say = state.step.querySelector(".say") || state.step.insertBefore(h("div", { class: "say" }), state.step.children[1] || null);
          say.textContent += ev.text;
        }
        break;
      case "retry":
        item("warn", "retry", "Retry with backoff: " + ev.name, "attempt " + a.attempt + " failed (" + a.error + "); wait " + a.wait_s + " s");
        break;
      case "tool_start":
        state.tool = item("tool", "tool call", ev.name, short(a.args || {}));
        state.tool.querySelector(".t").classList.add("spin");
        break;
      case "tool_end":
        if (state.tool) {
          state.tool.querySelector(".t").classList.remove("spin");
          var bits = [a.result || ev.status, a.ms + " ms"];
          if (a.cache === "hit") bits.push("cache hit");
          if (a.retries) bits.push(a.retries + " retries");
          if (a.path) bits.push("path: " + [].concat(a.path).join(" → "));
          if (a.error) bits.push(a.error);
          state.tool.appendChild(h("div", { class: "d", text: bits.join(" · ") }));
          if (ev.status === "error") state.tool.classList.add("warn");
        }
        break;
      case "decision":
        var f = DECISIONS[ev.name];
        if (f) { var r = f(a); item(r[0], "harness", r[1], r[2]); }
        break;
      case "approval_needed":
        showApproval(a);
        break;
      case "run_finished":
        finished(a);
        break;
      case "run_error":
        alertBox("Error: " + a.error);
        break;
    }
  }

  function showApproval(a) {
    var p = a.payload || {};
    var flags = p.flags || [];
    var box = h("div", { class: "approval" + (flags.length ? " flagged" : "") });
    box.appendChild(h("h3", { text: a.kind === "refund" ? "Approve this refund?" : "Approve this e-mail?" }));
    if (flags.length) box.appendChild(h("div", { class: "flag", text: "⚠ Security flags in this run: " + flags.map(function (f) { return f.type + " (" + (f.source || f.tool || "") + ")"; }).join(", ") }));
    var dl = h("dl");
    function row(k, v) { if (v !== undefined && v !== null && v !== "") { dl.appendChild(h("dt", { text: k })); dl.appendChild(h("dd", { text: String(v) })); } }
    if (a.kind === "refund") {
      row("Customer", p.customer); row("Order", p.args.order_id); row("Amount", money(p.args.amount_cents) + " (" + p.args.kind + ")");
      row("Order total", money(p.order_total_cents || 0)); row("Already refunded", money(p.already_refunded_cents || 0)); row("Reason", p.args.reason);
    } else {
      row("To", p.to); row("Action", p.action); if (p.refund_cents) row("Refunded", money(p.refund_cents));
      row("Policy", (p.policy_refs || []).join(", ")); row("Summary", p.summary);
      $("#reply-wrap").hidden = false;
      $("#reply").textContent = p.body;
      $("#reply-state").textContent = "rehydrated: the placeholders now hold the real values";
    }
    box.appendChild(dl);
    var reason = h("input", { placeholder: "Reason (optional)", maxlength: "300" });
    function decide(ok) {
      box.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
      api("/api/approvals/" + a.approval_id, { approved: ok, reason: reason.value || null }).then(function () {
        box.remove();
        listen();
      }).catch(function (err) { alertBox(err.message); });
    }
    box.appendChild(h("div", { class: "actions" }, [
      h("button", { class: "btn ok", type: "button", onclick: function () { decide(true); } }, "Approve"),
      h("button", { class: "btn warn", type: "button", onclick: function () { decide(false); } }, "Reject"), reason
    ]));
    $("#approval").textContent = "";
    $("#approval").appendChild(box);
    box.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function finished(a) {
    var btn = $("#run-btn");
    if (btn) btn.disabled = false;
    var res = a.resolution || {};
    var done = $("#done");
    done.hidden = false;
    done.textContent = "";
    var cls = a.status === "resolved" ? "ok" : a.status === "escalated" ? "tool" : "warn";
    done.appendChild(h("div", { class: "row" }, [h("span", { class: "chip " + cls, text: a.status }), " ",
      h("b", { text: (res.action || "") + (res.refunded_cents ? " · " + money(res.refunded_cents) : "") })]));
    if (res.summary || res.reason) done.appendChild(h("p", { class: "muted", text: res.summary || res.reason }));
    var fb = h("div", { class: "fb" }, [h("span", { text: "Was this run right?" })]);
    var comment = h("input", { placeholder: "What was wrong? (optional)", maxlength: "500" });
    function rate(n) {
      api("/api/runs/" + state.runId + "/feedback", { rating: n, comment: comment.value || null }).then(function (r) {
        fb.textContent = n > 0 ? "Thanks. Good runs can become fine-tuning data." : "Thanks. A draft eval case is in evals/inbox/" + (r.draft_case || "") + ".";
      });
    }
    fb.appendChild(h("button", { type: "button", onclick: function () { rate(1); } }, "👍"));
    fb.appendChild(h("button", { type: "button", onclick: function () { rate(-1); } }, "👎"));
    fb.appendChild(comment);
    done.appendChild(fb);
    $("#reply-state").textContent = res.email_sent ? "sent (to the demo outbox)" : "not sent";
    inspect(state.runId);
    loadRuns();
  }

  /* ---------- inspector ---------- */
  function marked(text) {
    var frag = document.createDocumentFragment();
    var re = /(<(?:NAME|FIRST_NAME|EMAIL|PHONE)_\d+>|\[(?:CARD|IBAN|API_KEY|PASSWORD)_REMOVED\]|<\/?untrusted_data[^>]*>)/g;
    var last = 0, m;
    while ((m = re.exec(text))) {
      frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      frag.appendChild(h("mark", { class: m[0].indexOf("untrusted") >= 0 ? "ut" : "ph", text: m[0] }));
      last = m.index + m[0].length;
    }
    frag.appendChild(document.createTextNode(text.slice(last)));
    return frag;
  }
  function inspect(runId) {
    api("/api/runs/" + runId).then(function (d) {
      var seen = $("#tab-seen");
      seen.textContent = "";
      seen.appendChild(h("p", { class: "hint", text: "Green = placeholders for personal data. Red = untrusted text. The model never saw the real values." }));
      d.run.state.messages.forEach(function (m) {
        var box = h("div", { class: "msg " + m.role }, [h("span", { class: "role", text: m.role })]);
        (Array.isArray(m.content) ? m.content : [{ type: "text", text: m.content }]).forEach(function (b) {
          var pre = h("pre");
          if (b.type === "text") pre.appendChild(marked(b.text));
          else if (b.type === "tool_use") pre.appendChild(marked("→ " + b.name + " " + JSON.stringify(b.input)));
          else if (b.type === "tool_result") pre.appendChild(marked((b.is_error ? "✗ " : "← ") + (typeof b.content === "string" ? b.content : JSON.stringify(b.content))));
          else if (b.type === "thinking") pre.appendChild(document.createTextNode("[thinking] " + (b.thinking || "").slice(0, 400)));
          else pre.appendChild(document.createTextNode("[" + b.type + "]"));
          box.appendChild(pre);
        });
        seen.appendChild(box);
      });

      var tr = $("#tab-trace");
      tr.textContent = "";
      var spans = d.spans;
      var t0 = Math.min.apply(null, spans.map(function (s) { return s.start_ts; }));
      var t1 = Math.max.apply(null, spans.map(function (s) { return s.end_ts || s.start_ts; }));
      var span = Math.max(0.001, t1 - t0);
      tr.appendChild(h("p", { class: "hint", text: "Blue = model, amber = tool, purple = harness decision. Width = time. Total " + (span).toFixed(2) + " s (wall time, includes the wait for approvals)." }));
      var wf = h("div", { class: "wf" });
      spans.filter(function (s) { return s.kind !== "run"; }).forEach(function (s) {
        var left = (s.start_ts - t0) / span * 100, width = Math.max(0.6, ((s.end_ts || s.start_ts) - s.start_ts) / span * 100);
        var label = s.kind === "model" ? "model" + (s.attrs.cache === "hit" ? " (cache)" : "") : s.name;
        wf.appendChild(h("div", { class: "r " + s.kind + (s.status === "error" ? " err" : ""), title: JSON.stringify(s.attrs) }, [
          h("span", { class: "n", text: label }), h("span", { class: "bar" }, h("i", { style: "left:" + left + "%;width:" + width + "%" }))
        ]));
      });
      tr.appendChild(wf);

      var sp = d.run.spend, b = (state.health || {}).budget || {};
      var pane = $("#tab-spend");
      pane.textContent = "";
      var sim = spans.some(function (s) { return s.attrs && s.attrs.simulated; });
      pane.appendChild(h("div", { class: "facts" }, [
        h("div", {}, [h("b", { text: usd(sp.cost_usd) }), h("span", { text: "cost of this task" + (sim ? " (simulated)" : "") })]),
        h("div", {}, [h("b", { text: sp.model_calls }), h("span", { text: "model calls" })]),
        h("div", {}, [h("b", { text: sp.usage.input_tokens + " / " + sp.usage.output_tokens }), h("span", { text: "tokens in / out" })]),
        h("div", {}, [h("b", { text: sp.cache_hits }), h("span", { text: "cache hits ($0 each)" })])
      ]));
      function meter(label, used, cap, fmt) {
        var pct = cap ? Math.min(100, used / cap * 100) : 0;
        pane.appendChild(h("div", { class: "meter" }, [h("span", { text: label + ": " + fmt(used) + " of " + fmt(cap) }),
          h("div", { class: "track" }, h("span", { style: "width:" + pct + "%" }))]));
      }
      if (b.steps) {
        pane.appendChild(h("h3", { text: "Budget for one run" }));
        meter("Steps", d.run.state.step, b.steps, String);
        meter("Tokens", sp.usage.input_tokens + sp.usage.output_tokens + sp.usage.cache_read_tokens + sp.usage.cache_write_tokens, b.tokens, String);
        meter("Cost", sp.cost_usd, b.cost_usd, usd);
        meter("Active time (s)", sp.active_seconds, b.seconds, function (x) { return Number(x).toFixed(1); });
      }
    });
  }
  function openRun(id) {
    reset();
    state.runId = id;
    api("/api/runs/" + id).then(function (d) {
      var t = d.run.ticket;
      pick(t);
      listen();   // replays the stored events of this run, if the server still has them
      inspect(id);
    });
  }

  /* ---------- start ---------- */
  api("/api/health").then(function (hl) {
    state.health = hl;
    var m = $("#mode");
    m.textContent = hl.mode + " model" + (hl.demo ? " · demo" : "");
    m.className = "chip " + (hl.mode === "offline" ? "tool" : "llm");
    m.title = "small route: " + hl.small_model + " · large route: " + hl.large_model + (hl.fallback ? " · fallback: " + hl.fallback : "");
  });
  api("/api/tickets").then(function (ts) { state.tickets = ts; renderTickets(); if (ts.length) pick(ts[0]); });
  loadRuns();
})();
