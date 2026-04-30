// Test buttons + Reload button + status polling. Plain DOM, no framework.

function showResult(target, ok, message) {
  const el = document.getElementById("result-" + target);
  if (!el) return;
  el.textContent = message;
  el.classList.remove("ok", "ko");
  el.classList.add(ok ? "ok" : "ko");
}

function showBanner(ok, message) {
  const banner = document.getElementById("banner");
  banner.textContent = message;
  banner.className = "alert " + (ok ? "success" : "error");
  banner.classList.remove("hidden");
  setTimeout(() => banner.classList.add("hidden"), 6000);
}

async function postForm(url, formData) {
  const response = await fetch(url, {
    method: "POST",
    body: formData,
    credentials: "same-origin",
  });
  if (!response.ok) {
    throw new Error("HTTP " + response.status);
  }
  return response.json();
}

document.querySelectorAll("button[data-test]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = button.dataset.test;
    const form = document.getElementById("settings-form");
    const data = new FormData(form);
    data.set("target", target);
    button.disabled = true;
    showResult(target, true, "Testing…");
    try {
      const result = await postForm("/test", data);
      const r = result[target];
      showResult(target, r.ok, r.message);
    } catch (err) {
      showResult(target, false, "Request failed: " + err.message);
    } finally {
      button.disabled = false;
    }
  });
});

const reloadBtn = document.getElementById("reload-btn");
if (reloadBtn) {
  reloadBtn.addEventListener("click", async () => {
    reloadBtn.disabled = true;
    try {
      const r = await postForm("/reload", new FormData());
      showBanner(r.ok, r.message);
      // Refresh status soon after reloading
      setTimeout(refreshStatus, 1000);
    } catch (err) {
      showBanner(false, "Reload failed: " + err.message);
    } finally {
      reloadBtn.disabled = false;
    }
  });
}

// -------------------- status panel --------------------
function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value == null ? "—" : value;
}

function renderStatus(payload) {
  const updated = document.getElementById("status-updated");
  if (!payload.available) {
    updated.textContent = payload.message || "unavailable";
    document.getElementById("status-state").textContent = "unavailable";
    return;
  }
  updated.textContent = "updated " + (payload.updated_age || "—");
  setText("status-state", payload.state);
  setText("status-cycles", payload.cycle_count);
  setText("status-interval", payload.interval_seconds + "s");
  setText("status-next", payload.next_cycle_age || "—");

  const last = payload.last_cycle;
  if (last) {
    setText("status-lastcycle", last.duration_seconds + "s @ " + (last.started_at || ""));
    setText("status-points", last.total_points);
    const tbody = document.getElementById("status-tbody");
    tbody.innerHTML = "";
    const cats = last.categories || {};
    Object.keys(cats).sort().forEach((cat) => {
      const c = cats[cat];
      const tr = document.createElement("tr");
      tr.innerHTML =
        "<td><code>" + cat + "</code></td>" +
        "<td>" + (c.resources_seen || 0) + "</td>" +
        "<td>" + (c.resources_kept || 0) + "</td>" +
        "<td>" + (c.points_written || 0) + "</td>" +
        "<td class='err'>" + (c.error ? c.error : "") + "</td>";
      if (!c.enabled) tr.classList.add("disabled");
      tbody.appendChild(tr);
    });
  } else {
    setText("status-lastcycle", "no cycle yet");
    setText("status-points", "—");
  }
}

async function refreshStatus() {
  try {
    const r = await fetch("/status", { credentials: "same-origin" });
    if (r.ok) renderStatus(await r.json());
  } catch (err) {
    /* ignore transient errors */
  }
}

if (document.getElementById("status-card")) {
  refreshStatus();
  setInterval(refreshStatus, 5000);
}
