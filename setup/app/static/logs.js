// Live log viewer. Tails first via /logs/tail, then follows via SSE.

const MAX_LINES = 5000;
const logsPane = document.getElementById("logs");
const filterInput = document.getElementById("filter");
const levelSelect = document.getElementById("level-filter");
const autoscroll = document.getElementById("autoscroll");
const pauseBtn = document.getElementById("pause-btn");
const clearBtn = document.getElementById("clear-btn");
const connState = document.getElementById("conn-state");

const LEVEL_ORDER = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3 };

let lines = [];          // all received lines (rolling buffer)
let paused = false;
let eventSource = null;

function lineLevel(line) {
  // Match the collector's log format: "YYYY-MM-DD HH:MM:SS,ms LEVEL ..."
  const m = line.match(/\b(DEBUG|INFO|WARNING|ERROR)\b/);
  return m ? m[1] : null;
}

function passFilter(line) {
  const f = filterInput.value.trim().toLowerCase();
  if (f && line.toLowerCase().indexOf(f) === -1) return false;

  const minLevel = levelSelect.value;
  if (minLevel) {
    const level = lineLevel(line);
    // Lines without a level (continuation lines) are kept unless we're
    // strictly filtering for ERROR.
    if (!level) return minLevel !== "ERROR";
    if (LEVEL_ORDER[level] < LEVEL_ORDER[minLevel]) return false;
  }
  return true;
}

function levelClass(line) {
  const lvl = lineLevel(line);
  return lvl ? "lvl-" + lvl : "";
}

function rerender() {
  // Build the visible content from scratch — simpler than tracking diffs,
  // and fast enough for a few thousand lines.
  const html = lines
    .filter(passFilter)
    .map((l) => {
      const cls = levelClass(l);
      return cls
        ? '<span class="' + cls + '">' + escapeHtml(l) + "</span>"
        : escapeHtml(l);
    })
    .join("\n");
  logsPane.innerHTML = html || "(no matching lines)";
  if (autoscroll.checked && !paused) {
    logsPane.scrollTop = logsPane.scrollHeight;
  }
}

function escapeHtml(s) {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function appendLine(line) {
  if (!line) return;
  lines.push(line);
  if (lines.length > MAX_LINES) lines.splice(0, lines.length - MAX_LINES);
  if (!paused && passFilter(line)) {
    const cls = levelClass(line);
    if (logsPane.textContent === "(no matching lines)") {
      logsPane.innerHTML = "";
    }
    const span = document.createElement("span");
    if (cls) span.className = cls;
    span.textContent = line;
    logsPane.appendChild(span);
    logsPane.appendChild(document.createTextNode("\n"));
    if (autoscroll.checked) {
      logsPane.scrollTop = logsPane.scrollHeight;
    }
  }
}

async function loadInitial() {
  try {
    const r = await fetch("/logs/tail?n=500", { credentials: "same-origin" });
    if (!r.ok) {
      logsPane.textContent = "Could not load logs: HTTP " + r.status;
      return;
    }
    const text = await r.text();
    lines = text.split("\n").filter((l) => l.length > 0);
    rerender();
  } catch (err) {
    logsPane.textContent = "Could not load logs: " + err.message;
  }
}

function startStream() {
  if (eventSource) eventSource.close();
  connState.textContent = "connecting…";
  eventSource = new EventSource("/logs/stream?tail=10");
  eventSource.onopen = () => {
    connState.textContent = "live";
    connState.classList.add("ok");
  };
  eventSource.onmessage = (event) => {
    appendLine(event.data);
  };
  eventSource.onerror = () => {
    connState.textContent = "reconnecting…";
    connState.classList.remove("ok");
    // EventSource auto-reconnects; nothing to do.
  };
}

filterInput.addEventListener("input", rerender);
levelSelect.addEventListener("change", rerender);

pauseBtn.addEventListener("click", () => {
  paused = !paused;
  pauseBtn.textContent = paused ? "Resume" : "Pause";
  if (!paused) rerender();
});

clearBtn.addEventListener("click", () => {
  lines = [];
  logsPane.textContent = "(cleared)";
});

(async () => {
  await loadInitial();
  startStream();
})();
