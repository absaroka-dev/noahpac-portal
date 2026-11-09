(function () {
  "use strict";
  var HELP = {
    standards: "Describe the condition and, if known, the AFSC or duty (for example: 'mild persistent asthma, 4N0X1, deployment'). You get the governing MSD lines, which categories they apply to, and what is still needed.",
    narsum: "Paste the de-identified clinical facts (history, findings, treatment, current limitations). You get a structured DRAFT NARSUM with the standards cited and a list of what is missing. You can reply to refine it.",
    waiver: "Name the waiver or action (PCS/overseas clearance, deployment, PRP, arming...) and what you already have. You get the required-items checklist from the loaded guidance, a gap check and a draft cover narrative.",
    ask: "Ask anything about the loaded publications (classification, standards, PRP, arming and use of force)."
  };
  var PLACEHOLDER = {
    standards: "Condition, AFSC/duty and context, e.g. 'History of mild persistent asthma, well controlled, 4N0X1, upcoming deployment'",
    narsum: "Clinical facts for the NARSUM, using [MEMBER] and [DATE] placeholders...",
    waiver: "Waiver type and what you have so far...",
    ask: "Your question..."
  };
  var mode = "standards", history = [], lastAnswerText = "";
  var log = document.getElementById("chat-log"), form = document.getElementById("ask-form"), input = document.getElementById("question-input");
  var submit = document.getElementById("ask-submit"), ack = document.getElementById("identifiers-removed");
  var newConv = document.getElementById("new-conversation"), count = document.getElementById("question-count"), help = document.getElementById("mode-help");
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".mode-tab"));

  function setMode(m) {
    if (m !== mode) { history = []; log.textContent = ""; newConv.hidden = true; }
    mode = m;
    tabs.forEach(function (t) { t.setAttribute("aria-selected", t.dataset.mode === m ? "true" : "false"); });
    help.textContent = HELP[m]; input.placeholder = PLACEHOLDER[m];
  }
  tabs.forEach(function (t) { t.addEventListener("click", function () { setMode(t.dataset.mode); }); });
  setMode("standards");
  input.addEventListener("input", function () { count.textContent = input.value.length + " / 6000"; });
  newConv.addEventListener("click", function () { history = []; log.textContent = ""; newConv.hidden = true; });

  function add(cls, text) {
    var d = document.createElement("div"); d.className = "msg " + cls; d.textContent = text; log.appendChild(d);
    d.scrollIntoView({ behavior: "smooth", block: "end" }); return d;
  }

  function renderAnswer(text) {  // text with [S#] tags, rendered without innerHTML
    var d = document.createElement("div"), re = /\[S(\d{1,2})\]/g, last = 0, m;
    while ((m = re.exec(text))) {
      d.appendChild(document.createTextNode(text.slice(last, m.index)));
      var s = document.createElement("sup"); s.className = "tag"; s.textContent = "[" + m[1] + "]"; d.appendChild(s);
      last = re.lastIndex;
    }
    d.appendChild(document.createTextNode(text.slice(last)));
    return d;
  }

  function block(title, items, cls) {
    if (!items || !items.length) return null;
    var b = document.createElement("div"); b.className = "msg-block " + (cls || "");
    var h = document.createElement("h3"); h.textContent = title; b.appendChild(h);
    var ul = document.createElement("ul");
    items.forEach(function (t) { var li = document.createElement("li"); li.textContent = t; ul.appendChild(li); });
    b.appendChild(ul); return b;
  }

  function addAnswer(data) {
    var d = document.createElement("div"); d.className = "msg msg-assistant";
    d.appendChild(renderAnswer(data.answer));
    var cites = (data.citations || []).map(function (c) {
      return "[" + c.tag.slice(1) + "] " + (c.cite || (c.doc + ", " + c.section + ", " + c.pages)) + " — " + (c.title ? c.title + " — " : "") +
        c.doc + " dated " + (c.pub_date || "unknown") + " (" + (c.date_basis || "?") + ")" + (c.cite ? ", " + c.pages : "");
    });
    var b1 = block("Sources to verify", cites); if (b1) d.appendChild(b1);
    var b2 = block("Check before use", data.warnings, "msg-warn"); if (b2) d.appendChild(b2);
    var disc = document.createElement("div"); disc.className = "msg-disclaimer"; disc.textContent = data.disclaimer || ""; d.appendChild(disc);
    var act = document.createElement("div"); act.className = "msg-actions";
    var full = data.answer + (cites.length ? "\n\nSources:\n" + cites.join("\n") : "") + "\n\n" + (data.disclaimer || "");
    var copy = document.createElement("button"); copy.type = "button"; copy.textContent = "Copy";
    copy.addEventListener("click", function () {
      (navigator.clipboard ? navigator.clipboard.writeText(full) : Promise.reject()).then(function () { copy.textContent = "Copied"; }, function () { copy.textContent = "Copy unavailable"; });
      setTimeout(function () { copy.textContent = "Copy"; }, 1800);
    });
    var dl = document.createElement("button"); dl.type = "button"; dl.textContent = "Download .txt";
    dl.addEventListener("click", function () {
      var a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([full], { type: "text/plain" }));
      a.download = "usaf-assistant-" + mode + ".txt"; document.body.appendChild(a); a.click(); a.remove();
    });
    act.appendChild(copy); act.appendChild(dl); d.appendChild(act);
    log.appendChild(d); d.scrollIntoView({ behavior: "smooth", block: "end" });
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();
    var q = input.value.trim();
    if (!q) return;
    if (!ack.checked) { ack.focus(); return; }
    input.value = ""; count.textContent = "0 / 6000"; submit.disabled = true;
    add("msg-user", q);
    var typing = add("typing", "Searching the publications…");
    try {
      var start = await fetch("/usaf-assistant/api/ask", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode: mode, question: q, conversation_history: history, identifiers_removed: true })
      });
      var sct = start.headers.get("content-type") || "";
      if (!sct.includes("application/json")) { typing.remove(); add("msg-error", "Your session has probably expired. Reload this page and sign in again."); return; }
      var started = await start.json();
      if (!start.ok || !started.job) { typing.remove(); add("msg-error", started.error || "Request failed (" + start.status + ")."); return; }
      // Answers take 10-40 s, longer than the 5 s the Cloudflare edge allows for one request, so poll for the result.
      var data = null, fails = 0, t0 = Date.now();
      while (Date.now() - t0 < 300000) {
        await new Promise(function (r) { setTimeout(r, 2000); });
        var poll;
        try { poll = await fetch("/usaf-assistant/api/result/" + encodeURIComponent(started.job), { cache: "no-store" }); fails = 0; }
        catch (e) { if (++fails > 5) throw e; continue; }
        var pct = poll.headers.get("content-type") || "";
        if (!pct.includes("application/json")) { typing.remove(); add("msg-error", "Your session has probably expired. Reload this page and sign in again."); return; }
        var pj = await poll.json();
        if (poll.status === 404) { typing.remove(); add("msg-error", pj.error || "The request expired. Send it again."); return; }
        if (pj.pending) { typing.textContent = "Searching the publications and writing… " + Math.round((Date.now() - t0) / 1000) + " s"; continue; }
        typing.remove();
        data = pj.result;
        if (pj.http_status !== 200 || data.error) { add("msg-error", data.error || "Request failed (" + pj.http_status + ")."); return; }
        break;
      }
      if (!data) { typing.remove(); add("msg-error", "No answer after 5 minutes. Try a shorter question."); return; }
      addAnswer(data);
      history.push({ role: "user", content: q }); history.push({ role: "assistant", content: data.answer });
      history = history.slice(-8); newConv.hidden = false;
    } catch (err) {
      typing.remove(); add("msg-error", "Network error: " + err.message);
    } finally { submit.disabled = false; input.focus(); }
  });
})();
