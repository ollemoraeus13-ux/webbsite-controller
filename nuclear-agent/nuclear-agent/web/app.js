/* Nuclear Agent control panel. Plain JS, no build step.
   All state lives in the backend; this page only polls it, so refreshing
   the panel never affects a running task. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const TOKEN = window.NUCLEAR_TOKEN;

  const ui = {
    taskId: null,        // task whose history is shown
    status: null,
    settings: null,
    olderCursor: null,
    tasksOffset: 0,
    shotBusy: false,
    shotUrl: null,
    lastActionsKey: "",
    viewport: { w: 1280, h: 800 },
  };

  // ------------------------------------------------------------ helpers
  async function api(path, { method = "GET", body, raw = false } = {}) {
    const opts = { method, headers: { "X-Nuclear-Token": TOKEN } };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (raw) return res;
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
    return data;
  }

  let toastTimer = null;
  function toast(msg, isErr = false) {
    const t = $("toast");
    t.textContent = msg;
    t.className = "toast" + (isErr ? " err" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add("hidden"), isErr ? 6000 : 2500);
  }

  async function act(fn, okMsg) {
    try { const r = await fn(); if (okMsg) toast(okMsg); refreshStatus(); return r; }
    catch (e) { toast(e.message, true); }
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString();
  const fmtDate = (t) => new Date(t * 1000).toLocaleString();
  const setPill = (id, text, cls) => { const p = $(id); p.textContent = text; p.className = "pill " + (cls || ""); };

  // ------------------------------------------------------------- status
  async function refreshStatus() {
    let s;
    try { s = await api("/api/status"); }
    catch (e) { setPill("pill-task", "Server offline", "bad"); return; }
    ui.status = s;
    const b = s.browser || {};
    setPill("pill-browser", b.running ? "Browser: on" : "Browser: off", b.running ? "ok" : "");
    if (b.running && document.activeElement !== $("url")) $("url").value = b.url || "";
    $("btn-browser").textContent = b.running ? "⏻ Stop browser" : "⏻ Start browser";
    $("ocr-state").textContent = s.ocr_available ? "(available)" : "(Tesseract not found)";

    const t = s.task;
    const st = t ? t.status : "idle";
    const active = s.busy;
    setPill("pill-task", "Task: " + st.replace("_", " "),
      { running: "ok", paused: "warn", awaiting_confirmation: "warn", needs_user: "warn", failed: "bad", completed: "ok" }[st] || "");

    $("btn-start").disabled = active;
    $("btn-pause").disabled = !(active && st === "running");
    $("btn-resume").disabled = !(active && (st === "paused" || st === "needs_user"));
    $("btn-stop").disabled = !active;

    if (t) {
      if (ui.taskId !== t.task_id) { ui.taskId = t.task_id; ui.lastActionsKey = ""; }
      $("status-text").textContent = `#${t.task_id} · ${st.replace("_", " ")} · step ${t.step}/${t.max_actions}` + (t.message ? ` — ${t.message}` : "");
      $("progress-bar").style.width = Math.min(100, (t.step / Math.max(1, t.max_actions)) * 100) + "%";
      $("summary").textContent = t.summary || "–";
      $("last-error").textContent = t.last_error || "–";
      const mem = $("memory"); mem.replaceChildren();
      (t.memory || []).forEach((m) => mem.appendChild(el("li", "", m)));
      if (!t.memory || !t.memory.length) mem.appendChild(el("li", "", "No notes yet."));
    } else {
      $("status-text").textContent = "No task running.";
    }

    // approval card
    const confirmCard = $("confirm-card");
    if (t && st === "awaiting_confirmation" && t.pending) {
      confirmCard.classList.remove("hidden");
      $("confirm-reason").textContent = t.pending.reason || "";
      $("confirm-action").textContent = JSON.stringify(t.pending.action, null, 2);
    } else confirmCard.classList.add("hidden");

    // user-needed card
    const userCard = $("user-card");
    if (t && st === "needs_user") {
      userCard.classList.remove("hidden");
      $("user-message").textContent = t.message || "";
    } else userCard.classList.add("hidden");

    // final result
    if (t && !active && (st === "completed" || st === "failed" || st === "stopped")) {
      loadResult(t.task_id);
    }
  }

  let resultShownFor = null;
  async function loadResult(id) {
    if (resultShownFor === id) return;
    resultShownFor = id;
    try {
      const task = await api(`/api/tasks/${id}`);
      const box = $("result");
      box.textContent = task.result || task.error || "(no result)";
      box.classList.remove("hidden"); $("result-h").classList.remove("hidden");
      refreshTasks();
    } catch (_) { resultShownFor = null; }
  }

  // --------------------------------------------------------- screenshot
  async function refreshScreenshot() {
    if (ui.shotBusy || document.hidden) return;
    const running = ui.status && ui.status.browser && ui.status.browser.running;
    if (!running) {
      $("screen").classList.add("hidden"); $("screen-empty").classList.remove("hidden");
      return;
    }
    ui.shotBusy = true;
    try {
      const res = await api("/api/screenshot", { raw: true });
      if (res.status === 200) {
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const img = $("screen");
        img.onload = () => {
          ui.viewport = { w: img.naturalWidth, h: img.naturalHeight };
          if (ui.shotUrl) URL.revokeObjectURL(ui.shotUrl);
          ui.shotUrl = url;
        };
        img.src = url;
        img.classList.remove("hidden"); $("screen-empty").classList.add("hidden");
      }
    } catch (_) { /* transient */ }
    finally { ui.shotBusy = false; }
  }

  $("screen").addEventListener("click", (ev) => {
    const img = ev.currentTarget, r = img.getBoundingClientRect();
    const x = Math.round((ev.clientX - r.left) * (ui.viewport.w / r.width));
    const y = Math.round((ev.clientY - r.top) * (ui.viewport.h / r.height));
    act(() => api("/api/browser/action", { method: "POST", body: { type: "click", x, y } }));
  });

  // ------------------------------------------------------------ history
  function renderAction(a) {
    const li = el("li", a.ok ? "" : "fail");
    li.appendChild(el("span", "k", `${a.step} · ${a.kind}`));
    const params = Object.assign({}, a.params || {}); delete params.type;
    if (Object.keys(params).length) li.appendChild(el("div", "", JSON.stringify(params)));
    if (a.reasoning) li.appendChild(el("div", "r", a.reasoning));
    if (a.outcome) li.appendChild(el("div", "", a.outcome));
    li.appendChild(el("div", "meta", fmtTime(a.created_at) + (a.url ? " · " + a.url : "")));
    return li;
  }

  async function refreshActions() {
    if (!ui.taskId) return;
    try {
      const { actions } = await api(`/api/tasks/${ui.taskId}/actions?limit=20`);
      const key = ui.taskId + ":" + (actions[0] ? actions[0].id : 0);
      if (key === ui.lastActionsKey) return;
      ui.lastActionsKey = key;
      const list = $("actions"); list.replaceChildren();
      if (!actions.length) list.appendChild(el("li", "", "No actions yet."));
      actions.forEach((a) => list.appendChild(renderAction(a)));
      ui.olderCursor = actions.length === 20 ? actions[actions.length - 1].id : null;
      $("btn-older").classList.toggle("hidden", !ui.olderCursor);
      if (!$("tab-checkpoints").classList.contains("hidden")) refreshCheckpoints();
    } catch (_) { }
  }

  $("btn-older").addEventListener("click", async () => {
    if (!ui.olderCursor) return;
    const { actions } = await api(`/api/tasks/${ui.taskId}/actions?limit=20&before=${ui.olderCursor}`);
    actions.forEach((a) => $("actions").appendChild(renderAction(a)));
    ui.olderCursor = actions.length === 20 ? actions[actions.length - 1].id : null;
    $("btn-older").classList.toggle("hidden", !ui.olderCursor);
  });

  async function refreshCheckpoints() {
    if (!ui.taskId) return;
    const { checkpoints } = await api(`/api/tasks/${ui.taskId}/checkpoints?limit=20`);
    const list = $("checkpoints"); list.replaceChildren();
    if (!checkpoints.length) list.appendChild(el("li", "", "No checkpoints yet."));
    checkpoints.forEach((c) => {
      const li = el("li");
      li.appendChild(el("span", "k", `step ${c.step} · ${c.label}`));
      li.appendChild(el("div", "", c.url || "(no url)"));
      const mem = (c.state && c.state.memory) || [];
      if (mem.length) li.appendChild(el("div", "r", "Notes: " + mem.join(" | ")));
      li.appendChild(el("div", "meta", fmtDate(c.created_at)));
      list.appendChild(li);
    });
  }

  async function refreshTasks() {
    const { tasks, total } = await api(`/api/tasks?limit=20&offset=${ui.tasksOffset}`);
    const list = $("tasks"); list.replaceChildren();
    if (!tasks.length) list.appendChild(el("li", "", "No tasks yet."));
    tasks.forEach((t) => {
      const li = el("li", t.status === "failed" ? "fail" : "");
      li.appendChild(el("span", "k", `#${t.id} · ${t.status}`));
      li.appendChild(el("div", "", t.goal));
      li.appendChild(el("div", "meta", `${t.steps}/${t.max_actions} actions · ${t.model || "?"} · ${fmtDate(t.created_at)}`));
      const row = el("div", "row");
      const view = el("button", "ghost", "View history");
      view.onclick = () => { ui.taskId = t.id; ui.lastActionsKey = ""; showTab("history"); refreshActions(); };
      row.appendChild(view);
      if (["interrupted", "stopped", "failed"].includes(t.status)) {
        const res = el("button", "", "Resume from checkpoint");
        res.onclick = () => act(() => api(`/api/tasks/${t.id}/resume`, { method: "POST" }), "Task resumed");
        row.appendChild(res);
      }
      li.appendChild(row);
      list.appendChild(li);
    });
    $("btn-tasks-prev").disabled = ui.tasksOffset === 0;
    $("btn-tasks-next").disabled = ui.tasksOffset + 20 >= total;
  }
  $("btn-tasks-prev").onclick = () => { ui.tasksOffset = Math.max(0, ui.tasksOffset - 20); refreshTasks(); };
  $("btn-tasks-next").onclick = () => { ui.tasksOffset += 20; refreshTasks(); };

  // --------------------------------------------------------------- tabs
  function showTab(name) {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    document.querySelectorAll(".tab-body").forEach((b) => b.classList.toggle("hidden", b.id !== "tab-" + name));
    if (name === "checkpoints") refreshCheckpoints().catch(() => {});
    if (name === "tasks") refreshTasks().catch(() => {});
    if (name === "settings") loadSettings();
  }
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

  // ----------------------------------------------------------- settings
  async function loadSettings() {
    try {
      ui.settings = await api("/api/settings");
      document.querySelectorAll("#settings [data-key]").forEach((inp) => {
        const [sec, key] = inp.dataset.key.split(".");
        const v = ui.settings[sec][key];
        if (inp.type === "checkbox") inp.checked = !!v; else inp.value = v;
      });
      if (!$("max-actions").value) $("max-actions").value = ui.settings.agent.max_actions;
      await loadModels();
    } catch (e) { toast(e.message, true); }
  }

  async function loadModels() {
    const info = $("models-info");
    info.textContent = "Checking Ollama…";
    let data;
    try { data = await api("/api/models"); } catch (e) { info.textContent = e.message; return; }
    const sel = $("s-model"), vsel = $("s-vision");
    sel.replaceChildren(); vsel.replaceChildren();
    sel.appendChild(new Option("(first installed model)", ""));
    vsel.appendChild(new Option("(none)", ""));
    if (!data.ok) {
      setPill("pill-ollama", "Ollama: offline", "bad");
      info.textContent = data.error + " — start Ollama and press Refresh.";
      return;
    }
    setPill("pill-ollama", `Ollama: ${data.models.length} model(s)`, data.models.length ? "ok" : "warn");
    data.models.forEach((m) => {
      const label = `${m.name} · ${m.parameters || "?"} · ${m.size_gb} GB${m.vision ? " · 👁 vision" : ""}`;
      sel.appendChild(new Option(label, m.name));
      if (m.vision) vsel.appendChild(new Option(label, m.name));
    });
    sel.value = (ui.settings && ui.settings.ollama.model) || "";
    vsel.value = (ui.settings && ui.settings.ollama.vision_model) || "";
    const nv = data.models.filter((m) => m.vision).length;
    info.textContent = data.models.length
      ? `${data.models.length} installed, ${nv} with vision. Models over ~6 GB may not fit in 8 GB VRAM.`
      : "No models installed. Run: ollama pull qwen2.5:7b";
  }
  $("btn-models").onclick = loadModels;

  $("settings").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const changes = { ollama: { model: $("s-model").value, vision_model: $("s-vision").value } };
    document.querySelectorAll("#settings [data-key]").forEach((inp) => {
      const [sec, key] = inp.dataset.key.split(".");
      (changes[sec] = changes[sec] || {})[key] = inp.type === "checkbox" ? inp.checked : inp.value;
    });
    try {
      ui.settings = await api("/api/settings", { method: "POST", body: changes });
      $("settings-msg").textContent = "Saved ✓";
      setTimeout(() => ($("settings-msg").textContent = ""), 2000);
    } catch (e) { toast(e.message, true); }
  });

  // ------------------------------------------------------------ buttons
  $("btn-start").onclick = () => {
    const goal = $("goal").value.trim();
    if (!goal) { toast("Describe the task first.", true); return; }
    resultShownFor = null;
    $("result").classList.add("hidden"); $("result-h").classList.add("hidden");
    act(() => api("/api/task", { method: "POST", body: {
      goal, start_url: $("start-url").value.trim(), max_actions: parseInt($("max-actions").value, 10) || null,
    } }), "Task started");
  };
  $("btn-pause").onclick = () => act(() => api("/api/task/pause", { method: "POST" }), "Pausing after current step");
  $("btn-resume").onclick = () => act(() => api("/api/task/resume", { method: "POST" }), "Resumed");
  $("btn-stop").onclick = () => act(() => api("/api/task/stop", { method: "POST" }), "Stopped");
  $("btn-approve").onclick = () => act(() => api("/api/task/confirm", { method: "POST", body: { approve: true } }), "Approved");
  $("btn-deny").onclick = () => act(() => api("/api/task/confirm", { method: "POST", body: { approve: false } }), "Denied");
  $("btn-reply").onclick = () => {
    const text = $("user-reply").value.trim();
    if (!text) return;
    act(() => api("/api/task/message", { method: "POST", body: { text } }), "Sent — press Resume to continue")
      .then(() => ($("user-reply").value = ""));
  };

  $("btn-browser").onclick = () => {
    const running = ui.status && ui.status.browser.running;
    act(() => api(running ? "/api/browser/stop" : "/api/browser/start", { method: "POST" }),
      running ? "Browser stopped" : "Browser started");
  };
  const go = () => {
    const url = $("url").value.trim();
    if (url) act(() => api("/api/browser/navigate", { method: "POST", body: { url } }));
  };
  $("btn-go").onclick = go;
  $("url").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  document.querySelectorAll("[data-manual]").forEach((b) =>
    b.addEventListener("click", () => act(() => api("/api/browser/action", { method: "POST", body: { type: b.dataset.manual } }))));
  document.querySelectorAll("[data-scroll]").forEach((b) =>
    b.addEventListener("click", () => act(() => api("/api/browser/action", { method: "POST", body: { type: "scroll", direction: b.dataset.scroll } }))));
  $("btn-type").onclick = () => {
    const text = $("manual-text").value;
    if (text) act(() => api("/api/browser/action", { method: "POST", body: { type: "type", text } })).then(() => ($("manual-text").value = ""));
  };
  $("btn-enter").onclick = () => act(() => api("/api/browser/action", { method: "POST", body: { type: "press", key: "Enter" } }));
  $("btn-inspect").onclick = async () => {
    const out = $("inspect-out");
    if (!out.classList.contains("hidden")) { out.classList.add("hidden"); return; }
    try {
      const o = await api("/api/browser/inspect");
      const lines = [`${o.title} — ${o.url}`, `CAPTCHA detected: ${o.captcha && o.captcha.detected ? "yes" : "no"}`, "", "ELEMENTS:"];
      o.elements.forEach((e) => lines.push(`[${e.id}] ${e.tag}${e.type ? "(" + e.type + ")" : ""} ${e.text || e.label || e.placeholder || ""}`));
      lines.push("", "TEXT:", o.text);
      out.textContent = lines.join("\n");
      out.classList.remove("hidden");
    } catch (e) { toast(e.message, true); }
  };

  // --------------------------------------------------------------- loop
  async function init() {
    await loadSettings();
    await refreshStatus();
    if (!ui.taskId) {
      try { const { tasks } = await api("/api/tasks?limit=1"); if (tasks[0]) ui.taskId = tasks[0].id; } catch (_) { }
    }
    refreshActions();
    setInterval(refreshStatus, 1000);
    setInterval(refreshActions, 2000);
    const tick = () => { refreshScreenshot(); setTimeout(tick, (ui.settings && ui.settings.ui.screenshot_refresh_ms) || 1500); };
    tick();
  }
  init();
})();
