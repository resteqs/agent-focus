// Agent Focus companion for Chromium browsers (Chrome, Arc, Brave, Edge, Vivaldi) and Firefox.
// While the local daemon reports `blocking`, tabs on blocked sites go to the wait page.
// The daemon has the final say: /api/intercept checks bypasses and starts the cooldown.
// If the daemon is not running, every request fails quietly and browsing is untouched.

const api = globalThis.browser ?? globalThis.chrome;
const BASE = "http://127.0.0.1:7842";
const STATE_TTL_MS = 2000;

let cached = null;
let cachedAt = 0;
let inflight = null;
const pending = new Set(); // tab ids with an intercept request in flight

async function getState() {
  if (cached && Date.now() - cachedAt < STATE_TTL_MS) return cached;
  if (!inflight) {
    inflight = fetch(`${BASE}/api/status`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .catch(() => null)
      .then((state) => {
        cached = state;
        cachedAt = Date.now();
        inflight = null;
        return state;
      });
  }
  return inflight;
}

function hostMatches(host, domain) {
  return host === domain || host.endsWith(`.${domain}`);
}

// A cheap local pre-check so only likely hits reach the daemon.
function looksBlocked(state, rawUrl) {
  if (!state?.blocking || !rawUrl) return false;
  let url;
  try {
    url = new URL(rawUrl);
  } catch (_) {
    return false;
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  const host = url.hostname.toLowerCase().replace(/^www\./, "");
  if (["127.0.0.1", "localhost", "[::1]", "::1"].includes(host)) return false;
  const bypassed = Object.keys(state.bypasses || {}).some((d) => hostMatches(host, d));
  if (bypassed) return false;
  return (state.config?.blocked_domains || []).some((d) => hostMatches(host, d));
}

async function checkTab(tabId, url) {
  if (pending.has(tabId)) return;
  const state = await getState();
  if (!looksBlocked(state, url)) return;
  pending.add(tabId);
  try {
    const res = await fetch(`${BASE}/api/intercept`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const verdict = res.ok ? await res.json() : null;
    if (verdict?.blocked && verdict.redirect_url) {
      await api.tabs.update(tabId, { url: verdict.redirect_url });
    }
  } catch (_) {
    // Daemon stopped or the tab closed; nothing to do.
  } finally {
    pending.delete(tabId);
  }
}

// Agent runs start without any tab event, so the alarm tick re-checks the active tabs while blocking.
async function sweepTabs() {
  const state = await getState();
  if (!state?.blocking) return;
  try {
    const tabs = await api.tabs.query({ active: true });
    for (const tab of tabs) if (tab.id !== undefined) checkTab(tab.id, tab.url);
  } catch (_) {}
}

api.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (change.url || change.status === "loading") checkTab(tabId, change.url || tab.url);
});

api.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await api.tabs.get(tabId);
    checkTab(tabId, tab.url);
  } catch (_) {}
});

// A one-minute alarm wakes the background worker; the sweep catches runs that began meanwhile.
api.alarms.create("agent-focus-tick", { periodInMinutes: 1 });
api.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "agent-focus-tick") sweepTabs();
});

api.runtime.onStartup?.addListener(sweepTabs);
api.runtime.onInstalled?.addListener(sweepTabs);
sweepTabs();
