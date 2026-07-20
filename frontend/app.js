const $ = (id) => document.getElementById(id);

// Inline raster style — same tiles the previous Leaflet build used, so it
// works in environments where CartoDB's vector style/tiles are blocked
// (corporate proxies, restricted networks). Swap to the vector style URL
// when running outside such environments for smoother zoom.
const CARTO_RASTER_STYLE = {
  version: 8,
  sources: {
    "carto-dark": {
      type: "raster",
      tiles: [
        "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "https://b.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "https://c.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      attribution: "© OpenStreetMap contributors © CARTO",
    },
  },
  layers: [
    { id: "background", type: "background", paint: { "background-color": "#0b1217" } },
    { id: "carto-dark", type: "raster", source: "carto-dark", paint: { "raster-opacity": 0.95 } },
  ],
  glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
};

let warMap = null;
let deckOverlay = null;
let twinCache = null;
let marketCtxCache = null;
let twinSummaryCache = null;
let latestLiveSignalTs = null;
let branchCache = null;   // most recent what-if branch overlay data
let lastPipelineDetails = null;
let liveAisPoints = [];
let liveAisLastFetchMs = 0;
let decisionModeEnabled = false;
let storyModeRunning = false;
let propagationRunId = 0;
let activeStoryFlowIndex = -1;
let lastRefreshSelection = null;
let currentPipelineStep = "collect";

const PIPELINE_STEP_META = [
  { key: "collect", pct: 17 },
  { key: "normalize", pct: 41 },
  { key: "twin", pct: 67 },
  { key: "reasoning", pct: 91 },
  { key: "recommendation", pct: 100 },
];

const mapLayersState = {
  routes: true,
  heat: true,
  ais: false,
  weatherAlerts: true,
  aisAlerts: true,
  procurement: true,
  branch: true,
};

const mapOverlayStats = {
  weatherAlerts: 0,
  aisAlerts: 0,
};

const mapOverlayDebug = {
  activeLayerIds: [],
  counts: {},
};

const LIVE_AIS_FETCH_TTL_MS = 45_000;


async function apiFetch(path, options = {}) {
  const { timeoutMs, ...fetchOptions } = options;
  const useTimeout = Number.isFinite(Number(timeoutMs)) && Number(timeoutMs) > 0;
  const controller = useTimeout ? new AbortController() : null;
  const timeoutHandle = useTimeout
    ? setTimeout(() => controller.abort(), Number(timeoutMs))
    : null;

  try {
    const res = await fetch(path, {
      ...fetchOptions,
      signal: controller ? controller.signal : fetchOptions.signal,
      headers: {
        "Content-Type": "application/json",
        ...(fetchOptions.headers || {}),
      },
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${text}`);
    }
    return res.json();
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new Error(`request timed out after ${Math.round(Number(timeoutMs) / 1000)}s: ${path}`);
    }
    throw err;
  } finally {
    if (timeoutHandle) clearTimeout(timeoutHandle);
  }
}

function setStatus(msg, tone = "idle") {
  const pill = $("statusPill");
  const raw = String(msg || "");
  const normalized = (() => {
    const m = raw.toLowerCase();
    if (tone === "ok") {
      if (m.includes("pipeline") || m.includes("refresh") || m.includes("loaded") || m.includes("trigger")) {
        return "Pipeline Ready";
      }
      return raw;
    }
    if (tone === "idle") {
      if (m.includes("booting") || m.includes("loading") || m.includes("refreshing") || m.includes("triggering")) {
        return "Updating Intelligence...";
      }
      return raw;
    }
    if (tone === "err") {
      if (m.includes("request timed out") || m.includes("http") || /\b\d{3}\b/.test(m) || m.includes("pipeline") || m.includes("event id") || m.includes("/")) {
        return "Needs attention. Please retry.";
      }
      return raw;
    }
    return raw;
  })();
  pill.textContent = normalized;
  pill.style.background = tone === "ok" ? "#c8ffd9" : tone === "err" ? "#ffd6dd" : "#0a141b";
  updateStatusTheme(tone);
  // Keep the power button in sync: only flip to err/idle — "running" and "on" are
  // set explicitly by triggerPipeline / refreshPipelineDetails.
  const btn = $("triggerBtn");
  if (btn) {
    if (tone === "err" && btn.dataset.state !== "running") btn.dataset.state = "err";
    else if (tone === "idle" && btn.dataset.state === "err") btn.dataset.state = "off";
  }
}

function updateStatusTheme(tone) {
  const pill = $("statusPill");
  if (tone === "ok") {
    pill.style.color = "#10321a";
    pill.style.borderColor = "#1f8540";
  } else if (tone === "err") {
    pill.style.color = "#4a0d14";
    pill.style.borderColor = "#ab2f3d";
  } else {
    pill.style.color = "#d7eaf0";
    pill.style.borderColor = "#29404c";
  }
}

function parseIsoMs(iso) {
  if (!iso) return NaN;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? NaN : t;
}

function formatRelativeAgeSeconds(sec, { short = true } = {}) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return short ? `${s} sec ago` : `${s} second${s === 1 ? "" : "s"} ago`;
  const m = Math.round(s / 60);
  if (s < 3600) return short ? `${m} min ago` : `${m} minute${m === 1 ? "" : "s"} ago`;
  const h = Math.round(s / 3600);
  if (s < 86400) return short ? `${h} hr ago` : `${h} hour${h === 1 ? "" : "s"} ago`;
  const d = Math.round(s / 86400);
  if (d < 14) return short ? `${d} day${d === 1 ? "" : "s"} ago` : `${d} day${d === 1 ? "" : "s"} ago`;
  const w = Math.round(d / 7);
  if (d < 60) return short ? `${w} wk ago` : `${w} week${w === 1 ? "" : "s"} ago`;
  return `${d} day${d === 1 ? "" : "s"} ago`;
}

function formatAge(iso) {
  const t = parseIsoMs(iso);
  if (!Number.isFinite(t)) return "n/a";
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  return formatRelativeAgeSeconds(sec, { short: true });
}

function formatAgeCompact(iso) {
  const t = parseIsoMs(iso);
  if (!Number.isFinite(t)) return "n/a";
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60) return `${sec} sec`;
  const min = Math.round(sec / 60);
  if (sec < 3600) return `${min} min`;
  const hr = Math.round(sec / 3600);
  if (sec < 86400) return `${hr} hr`;
  const day = Math.round(sec / 86400);
  return `${day} day${day === 1 ? "" : "s"}`;
}

function formatEtaCompact(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return `${s} sec`;
  const m = Math.round(s / 60);
  if (s < 3600) return `${m} min`;
  const h = Math.round(s / 3600);
  if (s < 86400) return `${h} hr`;
  const d = Math.round(s / 86400);
  return `${d} day${d === 1 ? "" : "s"}`;
}

function sourceStatusClass(status) {
  const s = String(status || "").toUpperCase();
  if (s === "LIVE") return "ops-live";
  if (s === "DEGRADED") return "ops-degraded";
  return "ops-stale";
}

function renderSourceHealthPanel(ctx) {
  const rowsEl = $("sourceHealthRows");
  if (!rowsEl) return;

  const toStatus = (iso, liveSec, warnSec) => {
    const cls = freshnessClass(iso, liveSec, warnSec);
    if (cls === "freshness-live") return { label: "Live", dot: "ops-live" };
    if (cls === "freshness-warn") return { label: "Degraded", dot: "ops-degraded" };
    if (cls === "freshness-unavailable") return { label: "Syncing", dot: "ops-degraded" };
    return { label: "Offline", dot: "ops-stale" };
  };

  const dtTs = twinSummaryCache?.as_of_utc || null;
  const aisTs = twinSummaryCache?.as_of_utc || null;
  const marketsTs = ctx?.price_last_update_utc || ctx?.latest_signal_utc || null;
  const weatherRow = Array.isArray(ctx?.source_health)
    ? ctx.source_health.find((r) => String(r?.label || "").toLowerCase() === "weather")
    : null;
  const weatherTs = weatherRow?.latest_utc || twinSummaryCache?.as_of_utc || null;
  const conflictTs = latestLiveSignalTs || ctx?.news_last_update_utc || null;

  const rows = [
    { label: "Digital Twin", status: toStatus(dtTs, 180, 1800) },
    { label: "AIS", status: toStatus(aisTs, 180, 1800) },
    { label: "Markets", status: toStatus(marketsTs, 172800, 1209600) },
    { label: "Weather", status: toStatus(weatherTs, 1800, 10800) },
    { label: "Conflict", status: toStatus(conflictTs, 43200, 172800) },
  ];

  if (!rows.length) {
    rowsEl.innerHTML = `<div class="ops-row"><span class="left"><i class="ops-dot ops-stale"></i>Awaiting source telemetry</span><span>n/a</span></div>`;
    return;
  }

  rowsEl.innerHTML = rows
    .map((r) => {
      const dot = r.status?.dot || "ops-stale";
      const text = r.status?.label || "Delayed";
      return `<div class="ops-row"><span class="left"><i class="ops-dot ${dot}"></i>${escapeHtml(r.label || "Source")}</span><span>${escapeHtml(text)}</span></div>`;
    })
    .join("");
}

function titleize(s) {
  return String(s || "")
    .replace(/_/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/\b\w/g, (m) => m.toUpperCase());
}

function renderTriggerExplainability(selectionMeta, mode = "") {
  const riskEl = $("selRiskScore");
  const freshEl = $("selFreshness");
  const confEl = $("selConfidence");
  const rankEl = $("selPriorityRank");
  const outEl = $("selOutranked");
  if (!riskEl || !freshEl || !confEl || !rankEl || !outEl) return;

  const sel = selectionMeta || null;
  if (!sel) {
    riskEl.textContent = "-";
    freshEl.textContent = "-";
    confEl.textContent = "-";
    rankEl.textContent = "-";
    outEl.innerHTML = "<li>Awaiting event selection...</li>";
    return;
  }

  const risk = Number(sel.score_100);
  riskEl.textContent = Number.isFinite(risk) ? String(risk) : "-";
  const ageMinutes = Number(sel.age_minutes);
  freshEl.textContent = Number.isFinite(ageMinutes)
    ? formatRelativeAgeSeconds(ageMinutes * 60, { short: true })
    : "n/a";

  const confLabel = String(sel.confidence_label || "").trim();
  const confNum = Number(sel.confidence);
  confEl.textContent = confLabel || (Number.isFinite(confNum) ? fmtPctWhole(confNum) : "-");
  const outrankedCount = Array.isArray(sel.outranked) ? sel.outranked.length : 0;
  rankEl.textContent = `#1 of ${Math.max(1, outrankedCount + 1)}`;

  const outranked = Array.isArray(sel.outranked) ? sel.outranked : [];
  if (!outranked.length) {
    const fallbackMsg = mode === "reused_latest_no_significant_news"
      ? "No significant candidate outranked the prior coherent event."
      : "No alternative candidates available.";
    outEl.innerHTML = `<li>${escapeHtml(fallbackMsg)}</li>`;
    return;
  }
  outEl.innerHTML = outranked
    .map((item) => `<li>${escapeHtml(titleize(item.label || `${item.action || "signal"}: ${item.target || "market"}`))}</li>`)
    .join("");
}

function freshnessClass(iso, liveSec = 120, warnSec = 1800) {
  const t = parseIsoMs(iso);
  if (!Number.isFinite(t)) return "freshness-unavailable";
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec <= liveSec) return "freshness-live";
  if (sec <= warnSec) return "freshness-warn";
  return "freshness-stale";
}

function renderFreshnessStrip() {
  const el = $("freshnessStrip");
  if (!el) return;

  const priceTs = marketCtxCache?.price_last_update_utc || marketCtxCache?.latest_signal_utc || null;
  const newsTs = latestLiveSignalTs || marketCtxCache?.news_last_update_utc || null;
  const twinTs = twinSummaryCache?.as_of_utc || null;
  const conflictTs = latestLiveSignalTs || marketCtxCache?.news_last_update_utc || null;

  const rows = [
    { label: "AIS", ts: twinTs, liveSec: 180, warnSec: 1800 },
    { label: "News", ts: newsTs, liveSec: 43200, warnSec: 172800 },
    { label: "Prices", ts: priceTs, liveSec: 172800, warnSec: 1209600 },
    { label: "Conflict", ts: conflictTs, liveSec: 43200, warnSec: 172800 },
  ];

  el.innerHTML = rows.map((r) => {
    const cls = freshnessClass(r.ts, r.liveSec, r.warnSec);
    const t = parseIsoMs(r.ts);
    const ageHours = Number.isFinite(t) ? Math.max(0, (Date.now() - t) / 3600000) : NaN;
    let status = cls === "freshness-live"
      ? "Live"
      : cls === "freshness-warn"
        ? "Degraded"
        : cls === "freshness-unavailable"
          ? "Syncing"
          : "Offline";
    if (r.label === "Prices" && cls === "freshness-warn" && Number.isFinite(ageHours)) {
      status = "Degraded";
    }
    if ((r.label === "News" || r.label === "Conflict") && cls === "freshness-warn") {
      status = "Degraded";
    }
    const ageText = cls === "freshness-unavailable" ? "syncing" : formatAgeCompact(r.ts);
    return `
      <div class="freshness-item">
        <span>${escapeHtml(r.label.toUpperCase())}</span>
        <strong><i class="freshness-dot ${cls}"></i>${escapeHtml(ageText)}</strong>
        <span>${status}</span>
      </div>`;
  }).join("");
}

function updateTopMissionBanner(details) {
  const topStatus = $("topStatus");
  const topReadiness = $("topReadiness");
  const topTwinSync = $("topTwinSync");
  if (!topStatus || !topReadiness || !topTwinSync) return;

  const state = details?.state || {};
  const conf = Number(state.reconciled_confidence ?? state.hypothesis_confidence ?? NaN);
  const disagree = !!state.disagreement;
  const objective = String($("missionObjective")?.textContent || "Maintain coverage of 1800 kbd").trim();
  const readiness = Number.isFinite(conf) && conf >= 0.6 && !disagree ? "Ready" : "Guarded";
  const status = disagree ? "Degraded" : "Live";

  topStatus.textContent = status;
  topReadiness.textContent = readiness;
  topTwinSync.textContent = twinSummaryCache?.as_of_utc ? `Synced ${formatAgeCompact(twinSummaryCache.as_of_utc)} ago` : "Syncing";
}

function setPipelineProgress(stepKey) {
  const el = $("pipelineProgress");
  if (!el) return;
  const order = PIPELINE_STEP_META.map((s) => s.key);
  const idx = order.indexOf(stepKey);
  currentPipelineStep = stepKey;
  for (const node of el.querySelectorAll("span[data-step]")) {
    const key = node.dataset.step || "";
    const i = order.indexOf(key);
    const stepMeta = PIPELINE_STEP_META.find((s) => s.key === key);
    const pctEl = node.querySelector(".pp-pct");
    node.classList.remove("active", "done", "attention");
    if (idx === -1) continue;
    if (i < idx) {
      node.classList.add("done");
      if (pctEl) pctEl.textContent = "Completed";
    } else if (i === idx) {
      node.classList.add("active");
      if (pctEl) pctEl.textContent = stepMeta ? `${stepMeta.pct}%` : "Running";
    } else {
      if (pctEl) pctEl.textContent = "Waiting";
    }
  }
}

function completePipelineProgress() {
  const el = $("pipelineProgress");
  if (!el) return;
  for (const node of el.querySelectorAll("span[data-step]")) {
    const key = node.dataset.step || "";
    const pctEl = node.querySelector(".pp-pct");
    node.classList.remove("active", "attention");
    node.classList.add("done");
    if (pctEl) pctEl.textContent = key === "recommendation" ? "Complete" : "Completed";
  }
}

function resetPipelineProgress() {
  const el = $("pipelineProgress");
  if (!el) return;
  for (const node of el.querySelectorAll("span[data-step]")) {
    const pctEl = node.querySelector(".pp-pct");
    node.classList.remove("active", "done", "attention");
    if (pctEl) pctEl.textContent = "Waiting";
  }
}

function markPipelineAttention(stepKey = currentPipelineStep) {
  const el = $("pipelineProgress");
  if (!el) return;
  const order = PIPELINE_STEP_META.map((s) => s.key);
  const idx = order.indexOf(stepKey);
  for (const node of el.querySelectorAll("span[data-step]")) {
    const key = node.dataset.step || "";
    const i = order.indexOf(key);
    const stepMeta = PIPELINE_STEP_META.find((s) => s.key === key);
    const pctEl = node.querySelector(".pp-pct");
    node.classList.remove("active", "done", "attention");
    if (idx !== -1 && i < idx) {
      node.classList.add("done");
      if (pctEl) pctEl.textContent = "Completed";
    } else if (i === idx) {
      node.classList.add("attention");
      if (pctEl) pctEl.textContent = stepMeta ? `${stepMeta.pct}% · Needs attention` : "Needs attention";
    } else if (pctEl) {
      pctEl.textContent = "Waiting";
    }
  }
}

function updateWorldStateFooter(details) {
  const state = details?.state || {};
  const conf = Number(state.reconciled_confidence ?? state.hypothesis_confidence ?? NaN);
  const counts = twinSummaryCache?.counts || {};
  const sources = [
    Number(counts.chokepoints || 0) > 0,
    Number(counts.routes || 0) > 0,
    Number(counts.ports || 0) > 0,
    Number(counts.refineries || 0) > 0,
    Number(counts.tankers || 0) > 0,
    Number(counts.countries || 0) > 0,
  ].filter(Boolean).length;

  const wsTwinStatus = $("wsTwinStatus");
  const wsSyncAgo = $("wsSyncAgo");
  const wsSources = $("wsSources");
  const wsEvents = $("wsEvents");
  const wsConfidence = $("wsConfidence");
  if (!wsTwinStatus || !wsSyncAgo || !wsSources || !wsEvents || !wsConfidence) return;

  wsTwinStatus.textContent = `Digital Twin: ${twinSummaryCache?.as_of_utc ? "Healthy" : "Degraded"}`;
  wsSyncAgo.textContent = formatAge(twinSummaryCache?.as_of_utc || null);
  wsSources.textContent = sources > 0 ? String(sources) : "-";
  wsEvents.textContent = marketCtxCache?.signal_count != null ? `${Number(marketCtxCache.signal_count).toLocaleString()}` : "-";
  wsConfidence.textContent = Number.isFinite(conf) ? fmtPctWhole(conf) : "-";
  updateTopMissionBanner(details);
}

// ---------------------------------------------------------------------------
// Map — MapLibre GL base + deck.gl overlay (HeatmapLayer + ArcLayer)
// ---------------------------------------------------------------------------

const COLOR = {
  low: [34, 197, 94],       // green
  medium: [245, 158, 11],   // amber
  high: [244, 63, 94],      // pink/red
  route: [93, 155, 167],    // muted cyan/teal (base route tone)
  refinery: [148, 233, 214],
  port: [126, 210, 214],
  portClosed: [244, 63, 94],
  refineryStarved: [244, 63, 94],
  chokepoint: [255, 200, 96],
  branch: [255, 143, 161],  // dim pink for what-if branch overlay
  weather: [255, 177, 64],
  aisAlert: [239, 68, 68],
};

function riskColor(score) {
  if (score >= 0.7) return COLOR.high;
  if (score >= 0.4) return COLOR.medium;
  return COLOR.low;
}

function toRGBA([r, g, b], a = 200) {
  return [r, g, b, a];
}

function findRouteByStep(step) {
  if (!step || !twinCache) return null;
  const routes = twinCache.routes || [];
  if (step.entity_id) {
    const exact = routes.find((r) => r.id === step.entity_id);
    if (exact) return exact;
  }
  const name = String(step.entity_name || "").toLowerCase();
  if (!name) return null;
  return routes.find((r) => r.id.toLowerCase().includes(name));
}

function deriveRouteEvidenceSets(details) {
  const predicted = new Set();
  const observed = new Set();
  const chain = details?.hypothesis?.causal_chain;
  const steps = chain?.steps || [];
  const affectedRoutes = chain?.affected?.routes || [];

  for (const s of steps) {
    if (s.entity_kind !== "route") continue;
    const route = findRouteByStep(s);
    const routeId = route?.id || s.entity_id || null;
    if (!routeId) continue;
    const ev = String(s.evidence_type || "").toLowerCase();
    if (ev === "predicted") predicted.add(routeId);
    if (ev === "observed") observed.add(routeId);
  }

  const hasPredictedChain = steps.some((s) => String(s.evidence_type || "").toLowerCase() === "predicted");
  for (const rid of affectedRoutes) {
    if (predicted.has(rid) || observed.has(rid)) continue;
    if (hasPredictedChain) predicted.add(rid);
    else observed.add(rid);
  }

  return { predicted, observed };
}

function initMap() {
  if (!window.maplibregl) {
    setStatus("maplibre failed to load", "err");
    return;
  }

  warMap = new maplibregl.Map({
    container: "warMap",
    style: CARTO_RASTER_STYLE,
    center: [58.5, 22.8],
    zoom: 3.6,
    attributionControl: { compact: true },
  });

  warMap.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");

  warMap.on("load", async () => {
    if (window.deck && deck.MapboxOverlay) {
      deckOverlay = new deck.MapboxOverlay({ interleaved: false, layers: [] });
      warMap.addControl(deckOverlay);
    }

    try {
      await hydrateMapFromTwin();
    } catch (err) {
      setStatus(`twin fetch failed: ${err.message}`, "err");
    }
  });
}

async function hydrateMapFromTwin(force = false) {
  if (!warMap) return;
  if (!warMap.isStyleLoaded()) {
    await new Promise((resolve) => warMap.once("load", resolve));
  }
  if (!twinCache || force) {
    twinCache = await apiFetch("/digital-twin/state");
  }
  paintTwinBaseLayers(twinCache);
  populateEntitySelect(twinCache);
  refreshDeckLayers();
}

// Populate the Entity Inspector dropdown from real twin entities (grouped).
function populateEntitySelect(twin) {
  const sel = $("entitySelect");
  if (!sel) return;
  const chokepoints = (twin.chokepoints || []).slice().sort((a, b) => a.name.localeCompare(b.name));
  const ports = (twin.ports || []).slice().sort((a, b) => a.name.localeCompare(b.name));
  const countries = (twin.countries || []).slice().sort((a, b) => a.name.localeCompare(b.name));

  const opt = (label, value) => `<option value="${escapeHtml(value)}">${escapeHtml(label)}</option>`;
  sel.innerHTML =
    '<option value="">— pick a chokepoint / port / country —</option>' +
    `<optgroup label="Chokepoints">${chokepoints.map((c) => opt(c.name, c.name)).join("")}</optgroup>` +
    `<optgroup label="Ports">${ports.map((p) => opt(`${p.name} (${p.country_iso3})`, p.name.split(" (")[0])).join("")}</optgroup>` +
    `<optgroup label="Countries">${countries.map((c) => opt(c.name, c.name)).join("")}</optgroup>`;
}

function paintTwinBaseLayers(twin) {
  const ports = twin.ports || [];
  const chokepoints = twin.chokepoints || [];
  const routes = twin.routes || [];
  const refineries = twin.refineries || [];
  const portById = Object.fromEntries(ports.map((p) => [p.id, p]));

  // ---- Route lines (colour by risk_score, offset for closed chokepoints) --
  const routeFC = {
    type: "FeatureCollection",
    features: routes
      .map((r) => {
        const o = portById[r.origin_port_id];
        const d = portById[r.destination_port_id];
        if (!o || !d) return null;
        const passesClosed = (r.chokepoint_ids || []).some((cid) => {
          const cp = chokepoints.find((c) => c.id === cid);
          return cp && cp.status === "closed";
        });
        return {
          type: "Feature",
          geometry: { type: "LineString", coordinates: [[o.lon, o.lat], [d.lon, d.lat]] },
          properties: {
            id: r.id,
            transit_days: r.transit_days,
            insurance: r.insurance_premium_multiplier,
            risk: r.risk_score,
            chokepoint_ids: (r.chokepoint_ids || []).join(","),
            closed: passesClosed,
            label: `${o.name} → ${d.name}`,
          },
        };
      })
      .filter(Boolean),
  };

  const portFC = {
    type: "FeatureCollection",
    features: ports.map((p) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [p.lon, p.lat] },
      properties: {
        id: p.id,
        name: p.name,
        country: p.country_iso3,
        congestion: p.congestion_pct,
        status: p.status,
        draft: p.draft_m,
      },
    })),
  };

  const refineryFC = {
    type: "FeatureCollection",
    features: refineries
      .filter((r) => r.lat != null && r.lon != null)
      .map((r) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [r.lon, r.lat] },
        properties: {
          id: r.id,
          name: r.name,
          operator: r.operator,
          capacity_kbd: r.capacity_kbd,
          utilization_pct: r.utilization_pct,
          status: r.status,
        },
      })),
  };

  const chokepointFC = {
    type: "FeatureCollection",
    features: chokepoints.map((c) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [c.lon, c.lat] },
      properties: {
        id: c.id,
        name: c.name,
        throughput_mbd: c.throughput_mbd,
        risk_score: c.risk_score,
        status: c.status,
      },
    })),
  };

  upsertGeoJSONSource("routes-src", routeFC);
  upsertGeoJSONSource("ports-src", portFC);
  upsertGeoJSONSource("refineries-src", refineryFC);
  upsertGeoJSONSource("chokepoints-src", chokepointFC);

  // Route arcs are rendered by deck.gl (refreshDeckLayers) so they follow
  // great-circle curves and encode risk in colour + height instead of being
  // shabby straight lines. MapLibre only keeps the port/refinery/chokepoint
  // markers.

  upsertLayer({
    id: "chokepoints-glow",
    type: "circle",
    source: "chokepoints-src",
    paint: {
      "circle-color": [
        "case",
        ["==", ["get", "status"], "closed"], "#f43f5e",
        ["step", ["get", "risk_score"], "#22c55e", 0.4, "#f59e0b", 0.7, "#f43f5e"],
      ],
      "circle-opacity": 0.22,
      "circle-radius": [
        "interpolate", ["linear"], ["get", "throughput_mbd"],
        1, 14,
        22, 44,
      ],
      "circle-blur": 0.65,
    },
  });

  upsertLayer({
    id: "chokepoints-layer",
    type: "circle",
    source: "chokepoints-src",
    paint: {
      "circle-color": [
        "case",
        ["==", ["get", "status"], "closed"], "#f43f5e",
        ["step", ["get", "risk_score"], "#22c55e", 0.4, "#f59e0b", 0.7, "#f43f5e"],
      ],
      "circle-radius": [
        "interpolate", ["linear"], ["get", "throughput_mbd"],
        1, 5,
        22, 9,
      ],
      "circle-stroke-color": "#0a141b",
      "circle-stroke-width": 1.5,
    },
  });

  upsertLayer({
    id: "ports-layer",
    type: "circle",
    source: "ports-src",
    paint: {
      "circle-color": [
        "case",
        ["==", ["get", "status"], "closed"], "#f43f5e",
        ["step", ["get", "congestion"], "#7ed2d6", 40, "#f59e0b", 70, "#f43f5e"],
      ],
      // Base size from draft (larger port = larger dot); congestion swells the halo.
      "circle-radius": [
        "interpolate", ["linear"], ["coalesce", ["get", "draft"], 12], 10, 3.5, 32, 6.5,
      ],
      "circle-stroke-color": "#0a141b",
      "circle-stroke-width": 1.5,
    },
  });

  // Optional secondary congestion halo — grows with congestion pct, dim.
  upsertLayer({
    id: "ports-congestion-halo",
    type: "circle",
    source: "ports-src",
    paint: {
      "circle-color": [
        "step", ["get", "congestion"], "rgba(0,0,0,0)", 20, "#f59e0b", 60, "#f43f5e",
      ],
      "circle-opacity": 0.28,
      "circle-radius": [
        "interpolate", ["linear"], ["get", "congestion"], 20, 6, 100, 20,
      ],
      "circle-blur": 0.5,
    },
  });

  // Refineries: distinct visual — larger sized by capacity, opacity by utilization,
  // with an outer glow ring so they read differently from ports.
  upsertLayer({
    id: "refineries-glow",
    type: "circle",
    source: "refineries-src",
    paint: {
      "circle-color": [
        "case",
        ["==", ["get", "status"], "offline"], "#f43f5e",
        "#94e9d6",
      ],
      "circle-opacity": 0.18,
      "circle-radius": [
        "interpolate", ["linear"], ["get", "capacity_kbd"], 100, 12, 1500, 34,
      ],
      "circle-blur": 0.55,
    },
  });

  upsertLayer({
    id: "refineries-layer",
    type: "circle",
    source: "refineries-src",
    paint: {
      "circle-color": [
        "case",
        ["==", ["get", "status"], "offline"], "#f43f5e",
        "#94e9d6",
      ],
      "circle-radius": [
        "interpolate", ["linear"], ["get", "capacity_kbd"], 100, 7, 1500, 18,
      ],
      "circle-opacity": [
        "interpolate", ["linear"], ["get", "utilization_pct"], 0, 0.35, 1, 0.95,
      ],
      "circle-stroke-color": "#0a141b",
      "circle-stroke-width": 2.5,
    },
  });

  bindLayerPopup("ports-layer", (p) =>
    `<strong>${p.name}</strong><br/>country: ${p.country}<br/>congestion: ${Number(p.congestion).toFixed(1)}%<br/>status: <em>${p.status}</em>`
  );
  bindLayerPopup("refineries-layer", (p) =>
    `<strong>${p.name}</strong><br/>${p.operator}<br/>capacity: ${p.capacity_kbd} kbd<br/>utilization: ${(p.utilization_pct * 100).toFixed(0)}%<br/>status: <em>${p.status}</em>`
  );
  bindLayerPopup("chokepoints-layer", (p) =>
    `<strong>${p.name}</strong><br/>throughput: ${p.throughput_mbd} mbd<br/>risk: ${Number(p.risk_score).toFixed(2)}<br/>status: <em>${p.status}</em>`
  );

  // Focus highlight source is initialised empty; populated by focusMapOnEntity.
  if (!warMap.getSource("focus-src")) {
    warMap.addSource("focus-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  }
  upsertLayer({
    id: "focus-halo",
    type: "circle",
    source: "focus-src",
    paint: {
      "circle-color": "#f59e0b",
      "circle-opacity": 0.32,
      "circle-radius": 26,
      "circle-blur": 0.4,
    },
  });
  upsertLayer({
    id: "focus-ring",
    type: "circle",
    source: "focus-src",
    paint: {
      "circle-color": "rgba(0,0,0,0)",
      "circle-radius": 14,
      "circle-stroke-color": "#f59e0b",
      "circle-stroke-width": 2,
      "circle-stroke-opacity": 0.9,
    },
  });

  if (!warMap.getSource("prop-route-src")) {
    warMap.addSource("prop-route-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  }
  if (!warMap.getSource("prop-point-src")) {
    warMap.addSource("prop-point-src", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  }

  upsertLayer({
    id: "prop-route-layer",
    type: "line",
    source: "prop-route-src",
    paint: {
      "line-color": "#fbbf24",
      "line-width": 3.2,
      "line-opacity": 0.88,
    },
  });

  upsertLayer({
    id: "prop-point-halo",
    type: "circle",
    source: "prop-point-src",
    paint: {
      "circle-color": "#f59e0b",
      "circle-opacity": 0.3,
      "circle-radius": 24,
      "circle-blur": 0.55,
    },
  });

  upsertLayer({
    id: "prop-point-core",
    type: "circle",
    source: "prop-point-src",
    paint: {
      "circle-color": "#fbbf24",
      "circle-radius": 6,
      "circle-stroke-color": "#0a141b",
      "circle-stroke-width": 1.8,
      "circle-opacity": 0.95,
    },
  });

  // Chokepoints clickable to load their history
  warMap.on("click", "chokepoints-layer", (e) => {
    const p = e.features?.[0]?.properties;
    if (!p) return;
    const sel = $("entitySelect");
    if (sel) sel.value = p.name;
    showIncidents(p.name).catch((err) => setStatus(err.message, "err"));
  });
}

function coordsForRefinery() {
  // Removed: refineries now carry their own lat/lon from the twin.
  throw new Error("coordsForRefinery is deprecated — use refinery.lat / refinery.lon");
}

function upsertGeoJSONSource(id, data) {
  const existing = warMap.getSource(id);
  if (existing) {
    existing.setData(data);
  } else {
    warMap.addSource(id, { type: "geojson", data });
  }
}

function upsertLayer(spec) {
  if (warMap.getLayer(spec.id)) {
    warMap.removeLayer(spec.id);
  }
  warMap.addLayer(spec);
}

function bindLayerPopup(layerId, formatter) {
  // Hover-driven popup: show on mouseenter, follow on mousemove, dismiss on mouseleave.
  const popup = new maplibregl.Popup({
    closeButton: false,
    closeOnClick: false,
    className: "war-popup",
    offset: 12,
  });
  const showFor = (e) => {
    const feat = e.features?.[0];
    if (!feat) return;
    const coords = feat.geometry.type === "Point"
      ? feat.geometry.coordinates
      : feat.geometry.coordinates[Math.floor(feat.geometry.coordinates.length / 2)];
    popup.setLngLat(coords).setHTML(formatter(feat.properties)).addTo(warMap);
  };
  warMap.on("mouseenter", layerId, (e) => {
    warMap.getCanvas().style.cursor = "pointer";
    showFor(e);
  });
  warMap.on("mousemove", layerId, showFor);
  warMap.on("mouseleave", layerId, () => {
    warMap.getCanvas().style.cursor = "";
    popup.remove();
  });
}

function ensureDeckOverlay() {
  if (deckOverlay) return true;
  if (!warMap || !window.deck || !deck.MapboxOverlay) return false;
  try {
    deckOverlay = new deck.MapboxOverlay({ interleaved: false, layers: [] });
    warMap.addControl(deckOverlay);
    return true;
  } catch {
    return false;
  }
}

async function ensureLiveAisPoints(force = false) {
  if (!mapLayersState.ais) return;
  const ageMs = Date.now() - Number(liveAisLastFetchMs || 0);
  if (!force && liveAisPoints.length && ageMs < LIVE_AIS_FETCH_TTL_MS) {
    return;
  }
  try {
    const points = await apiFetch("/signals/ais-live?limit=900", { timeoutMs: 20000 });
    liveAisPoints = Array.isArray(points) ? points : [];
    liveAisLastFetchMs = Date.now();
  } catch {
    // Keep fallback behavior (Digital Twin tankers) when live AIS endpoint fails.
    liveAisPoints = [];
  }
}

function refreshDeckLayers() {
  if (!deckOverlay) {
    ensureDeckOverlay();
  }
  if (!deckOverlay || !twinCache) {
    window.__kavachMapDebug = {
      blocked: !deckOverlay ? "no_deck_overlay" : "no_twin_cache",
      activeLayerIds: [],
      counts: {},
      state: { ...mapLayersState },
      overlayStats: { ...mapOverlayStats },
    };
    return;
  }

  const layers = [];
  const portById = Object.fromEntries((twinCache.ports || []).map((p) => [p.id, p]));
  const routeEvidence = deriveRouteEvidenceSets(lastPipelineDetails);
  const debugCounts = {
    routes: 0,
    chokepoints: 0,
    tankers: 0,
    weatherAlerts: 0,
    aisAlerts: 0,
    procurementArcs: 0,
    branchArcs: 0,
  };

  // ---- Route arcs (great-circle curves; colour by risk, height + width by traffic) ----
  if (mapLayersState.routes && deck.ArcLayer) {
    const routes = (twinCache.routes || [])
      .map((r) => {
        const o = portById[r.origin_port_id];
        const d = portById[r.destination_port_id];
        if (!o || !d) return null;
        const closed = (r.chokepoint_ids || []).some((cid) => {
          const cp = (twinCache.chokepoints || []).find((c) => c.id === cid);
          return cp && cp.status === "closed";
        });
        return {
          source: [o.lon, o.lat],
          target: [d.lon, d.lat],
          risk: r.risk_score || 0,
          transit_days: r.transit_days,
          insurance: r.insurance_premium_multiplier,
          closed,
          predicted: routeEvidence.predicted.has(r.id),
          id: r.id,
          label: `${o.name} → ${d.name}`,
        };
      })
      .filter(Boolean);

    if (routes.length) {
      debugCounts.routes = routes.length;
      layers.push(
        new deck.ArcLayer({
          id: "route-arcs",
          data: routes,
          getSourcePosition: (d) => d.source,
          getTargetPosition: (d) => d.target,
          getSourceColor: (d) =>
            d.closed ? [244, 63, 94, 200]
              : d.predicted ? [251, 191, 36, 150]
              : d.risk >= 0.7 ? [244, 63, 94, 175]
              : d.risk >= 0.4 ? [245, 158, 11, 160]
              : [79, 128, 153, 120],
          getTargetColor: (d) =>
            d.closed ? [244, 63, 94, 210]
              : d.predicted ? [250, 204, 21, 165]
              : d.risk >= 0.7 ? [244, 63, 94, 185]
              : d.risk >= 0.4 ? [245, 158, 11, 170]
              : [98, 156, 186, 128],
          getWidth: (d) => (d.closed ? 2.8 : d.predicted ? 1.5 : 0.9 + Math.min(1.8, d.risk * 1.8)),
          getHeight: (d) => (d.closed ? 0.58 : d.predicted ? 0.48 : 0.22 + d.risk * 0.24),
          greatCircle: true,
          pickable: true,
          onClick: ({ object }) => {
            if (!object) return;
            new maplibregl.Popup({ className: "war-popup" })
              .setLngLat([(object.source[0] + object.target[0]) / 2, (object.source[1] + object.target[1]) / 2])
              .setHTML(
                `<strong>${escapeHtml(object.label)}</strong><br/>mode: ${object.predicted ? "predicted" : "observed"}<br/>transit: ${object.transit_days}d<br/>insurance ×${Number(object.insurance).toFixed(2)}<br/>risk: ${Number(object.risk).toFixed(2)}${object.closed ? "<br/><em>chokepoint closed</em>" : ""}`
              )
              .addTo(warMap);
          },
        })
      );
    }
  }

  // ---- HeatmapLayer over chokepoint risk ----
  const cps = (twinCache.chokepoints || []).map((c) => ({
    coords: [c.lon, c.lat],
    weight: (c.status === "closed" ? 1.0 : c.risk_score) || 0.1,
  }));
  debugCounts.chokepoints = cps.length;
  if (mapLayersState.heat && cps.length && deck.HeatmapLayer) {
    layers.push(
      new deck.HeatmapLayer({
        id: "chokepoint-heat",
        data: cps,
        getPosition: (d) => d.coords,
        getWeight: (d) => d.weight,
        radiusPixels: 55,
        intensity: 1.4,
        threshold: 0.05,
        colorRange: [
          [34, 197, 94, 30],
          [245, 158, 11, 130],
          [244, 63, 94, 200],
        ],
      })
    );
  }

  // ---- AIS tankers (live API points when enabled; fallback to twin slice) ----
  const liveTankers = (liveAisPoints || [])
    .filter((t) => t && t.lat != null && t.lon != null)
    .map((t) => ({
      ...t,
      dwt: t.dwt ?? 50000,
      destination_port_id: t.destination_port_id || "unknown",
    }));
  const twinTankers = (twinCache.tankers || [])
    .filter((t) => t && t.lat != null && t.lon != null)
    .map((t) => ({
      ...t,
      coords: [Number(t.lon), Number(t.lat)],
      size: Math.max(20, Math.min(80, Number(t.dwt || 0) / 5000)),
    }));
  const tankers = (mapLayersState.ais && liveTankers.length ? liveTankers : twinTankers)
    .map((t) => ({
      ...t,
      coords: [Number(t.lon), Number(t.lat)],
      size: Math.max(20, Math.min(80, Number(t.dwt || 0) / 5000)),
    }));
  debugCounts.tankers = tankers.length;
  if (mapLayersState.ais && tankers.length && deck.ScatterplotLayer) {
    layers.push(
      new deck.ScatterplotLayer({
        id: "ais-tankers",
        data: tankers,
        getPosition: (d) => d.coords,
        getRadius: (d) => d.size,
        radiusUnits: "meters",
        radiusMinPixels: 3,
        radiusMaxPixels: 10,
        stroked: true,
        filled: true,
        lineWidthMinPixels: 1,
        getFillColor: (d) =>
          d.status === "laden"
            ? [56, 189, 248, 220]
            : d.status === "ballast"
              ? [99, 102, 241, 220]
              : d.status === "anchored"
                ? [167, 139, 250, 230]
                : [148, 163, 184, 195],
        getLineColor: () => [214, 236, 255, 210],
        pickable: true,
        onClick: ({ object }) => {
          if (!object) return;
          const dwt = object.dwt != null ? `${Number(object.dwt).toFixed(0)} DWT` : "DWT n/a";
          const dest = object.destination_port_id || "unknown";
          new maplibregl.Popup({ className: "war-popup" })
            .setLngLat(object.coords)
            .setHTML(
              `<strong>${escapeHtml(object.name || object.mmsi)}</strong><br/>MMSI: ${escapeHtml(object.mmsi || "-")}<br/>status: ${escapeHtml(object.status || "unknown")}<br/>${escapeHtml(dwt)}<br/>destination: ${escapeHtml(dest)}`
            )
            .addTo(warMap);
        },
      })
    );
  }

  // ---- Weather alert overlay (ops + weather proxy, per chokepoint/port) ----
  const weatherAlerts = buildWeatherAlerts(twinCache, marketCtxCache);
  mapOverlayStats.weatherAlerts = weatherAlerts.length;
  debugCounts.weatherAlerts = weatherAlerts.length;
  if (mapLayersState.weatherAlerts && weatherAlerts.length && deck.ScatterplotLayer) {
    layers.push(
      new deck.ScatterplotLayer({
        id: "weather-alerts",
        data: weatherAlerts,
        getPosition: (d) => d.coords,
        getRadius: (d) => 30000 + Math.min(45000, d.severity * 45000),
        radiusUnits: "meters",
        radiusMinPixels: 6,
        radiusMaxPixels: 30,
        stroked: true,
        filled: true,
        lineWidthMinPixels: 2,
        getFillColor: (d) => [255, 177, 64, Math.round(55 + d.severity * 75)],
        getLineColor: (d) => [255, 204, 128, Math.round(160 + d.severity * 60)],
        pickable: true,
        onClick: ({ object }) => {
          if (!object) return;
          new maplibregl.Popup({ className: "war-popup" })
            .setLngLat(object.coords)
            .setHTML(
              `<strong>${escapeHtml(object.name)}</strong><br/>weather/ops stress: ${escapeHtml(object.level)}<br/>${escapeHtml(object.detail)}`
            )
            .addTo(warMap);
        },
      })
    );
  }

  // ---- AIS corridor anomaly overlay (anchoring buildup near chokepoints) ----
  const aisAlerts = buildAisCorridorAlerts(twinCache, tankers);
  mapOverlayStats.aisAlerts = aisAlerts.length;
  debugCounts.aisAlerts = aisAlerts.length;
  if (mapLayersState.aisAlerts && aisAlerts.length && deck.ScatterplotLayer) {
    layers.push(
      new deck.ScatterplotLayer({
        id: "ais-alerts",
        data: aisAlerts,
        getPosition: (d) => d.coords,
        getRadius: (d) => 35000 + Math.min(65000, d.severity * 50000),
        radiusUnits: "meters",
        radiusMinPixels: 7,
        radiusMaxPixels: 34,
        stroked: true,
        filled: false,
        lineWidthMinPixels: 2.4,
        getLineColor: (d) => [239, 68, 68, Math.round(165 + d.severity * 70)],
        pickable: true,
        onClick: ({ object }) => {
          if (!object) return;
          new maplibregl.Popup({ className: "war-popup" })
            .setLngLat(object.coords)
            .setHTML(
              `<strong>${escapeHtml(object.name)}</strong><br/>AIS anomaly: ${escapeHtml(object.level)}<br/>${escapeHtml(object.detail)}`
            )
            .addTo(warMap);
        },
      })
    );
  }

  // ---- Procurement arcs (import flows for the selected pipeline) ----
  if (mapLayersState.procurement && lastPipelineDetails && deck.ArcLayer) {
    const arcs = buildProcurementArcs(lastPipelineDetails, twinCache);
    if (arcs.length) {
      debugCounts.procurementArcs = arcs.length;
      layers.push(
        new deck.ArcLayer({
          id: "procurement-arcs",
          data: arcs,
          getSourcePosition: (d) => d.source,
          getTargetPosition: (d) => d.target,
          getSourceColor: () => [129, 140, 248, 210],
          getTargetColor: () => [167, 139, 250, 220],
          getWidth: (d) => 2 + Math.min(6, d.allocated_kbd / 200),
          getHeight: () => 0.9,
          greatCircle: true,
        })
      );
    }
  }

  // ---- What-if branch overlay ----
  if (mapLayersState.branch && branchCache && branchCache.routes_changed?.length && deck.ArcLayer) {
    const branchArcs = branchCache.routes_changed
      .map((rc) => {
        const route = (twinCache.routes || []).find((r) => r.id === rc.id);
        if (!route) return null;
        const o = portById[route.origin_port_id];
        const d = portById[route.destination_port_id];
        if (!o || !d) return null;
        return { source: [o.lon, o.lat], target: [d.lon, d.lat], id: rc.id };
      })
      .filter(Boolean);
    if (branchArcs.length) {
      debugCounts.branchArcs = branchArcs.length;
      layers.push(
        new deck.ArcLayer({
          id: "branch-arcs",
          data: branchArcs,
          getSourcePosition: (d) => d.source,
          getTargetPosition: (d) => d.target,
          getSourceColor: () => [255, 143, 161, 220],
          getTargetColor: () => [244, 63, 94, 220],
          getWidth: () => 4,
          getHeight: () => 1.2,
          greatCircle: true,
        })
      );
    }
  }

  renderMapOverlayBadges();
  mapOverlayDebug.activeLayerIds = layers.map((l) => l.id);
  mapOverlayDebug.counts = debugCounts;
  window.__kavachMapDebug = {
    ...mapOverlayDebug,
    state: { ...mapLayersState },
    overlayStats: { ...mapOverlayStats },
  };
  deckOverlay.setProps({ layers });
}

function renderMapOverlayBadges() {
  const el = $("mapOverlayBadges");
  if (!el) return;
  const badges = [];
  if (mapLayersState.weatherAlerts) {
    badges.push(`<span class="chip ${mapOverlayStats.weatherAlerts ? "chip-warn" : ""}">Weather alerts: ${mapOverlayStats.weatherAlerts}</span>`);
  }
  if (mapLayersState.aisAlerts) {
    badges.push(`<span class="chip ${mapOverlayStats.aisAlerts ? "chip-danger" : ""}">AIS anomalies: ${mapOverlayStats.aisAlerts}</span>`);
  }
  el.innerHTML = badges.join("") || `<span class="chip">No dynamic overlays</span>`;
}

function syncMapLayerControlsUI() {
  const root = $("mapLayerControls");
  if (!root) return;
  root.querySelectorAll("input[type='checkbox'][data-layer]").forEach((input) => {
    const key = input.dataset.layer;
    if (key in mapLayersState) input.checked = !!mapLayersState[key];
  });
}

function setDecisionMode(on) {
  decisionModeEnabled = !!on;
  const mapCard = $("mapCard");
  if (mapCard) mapCard.classList.toggle("decision-mode", decisionModeEnabled);

  if (decisionModeEnabled) {
    // Executive mode: keep only layers that support a direct decision.
    mapLayersState.routes = true;
    mapLayersState.heat = false;
    mapLayersState.ais = false;
    mapLayersState.weatherAlerts = true;
    mapLayersState.aisAlerts = true;
    mapLayersState.procurement = true;
    mapLayersState.branch = true;
  } else if (lastPipelineDetails) {
    applyContextAwareMapMode(lastPipelineDetails);
    return;
  }

  syncMapLayerControlsUI();
  refreshDeckLayers();
}

function inferEventContextType(details) {
  const text = String(details?.hypothesis?.hypothesis_text || "").toLowerCase();
  const steps = details?.hypothesis?.causal_chain?.steps || [];
  const mechanisms = steps.map((s) => String(s.mechanism || "").toLowerCase()).join(" ");
  const merged = `${text} ${mechanisms}`;

  if (/cyclone|storm|weather|monsoon|flood|typhoon/.test(merged)) return "weather";
  if (/conflict|war|strike|attack|sanction|embargo|ofac/.test(merged)) return "conflict";
  if (/ais|shipping|route|chokepoint|port|freight|insurance/.test(merged)) return "shipping";
  if (/inventory|draw|build|benchmark|brent|wti|price/.test(merged)) return "inventory";
  return "generic";
}

function applyContextAwareMapMode(details) {
  if (decisionModeEnabled || !details) return;
  const mode = inferEventContextType(details);
  const label = $("mapModeLabel");

  if (mode === "shipping") {
    mapLayersState.routes = true;
    mapLayersState.ais = true;
    mapLayersState.heat = true;
    mapLayersState.weatherAlerts = true;
    mapLayersState.aisAlerts = true;
  } else if (mode === "weather") {
    mapLayersState.routes = true;
    mapLayersState.ais = true;
    mapLayersState.heat = true;
    mapLayersState.weatherAlerts = true;
    mapLayersState.aisAlerts = false;
  } else if (mode === "inventory") {
    mapLayersState.routes = true;
    mapLayersState.ais = true;
    mapLayersState.heat = true;
    mapLayersState.weatherAlerts = true;
    mapLayersState.aisAlerts = true;
  } else if (mode === "conflict") {
    mapLayersState.routes = true;
    mapLayersState.ais = true;
    mapLayersState.heat = true;
    mapLayersState.weatherAlerts = true;
    mapLayersState.aisAlerts = true;
  } else {
    mapLayersState.routes = true;
    mapLayersState.ais = true;
    mapLayersState.heat = true;
    mapLayersState.weatherAlerts = true;
    mapLayersState.aisAlerts = true;
  }

  syncMapLayerControlsUI();
  refreshDeckLayers();
  if (label) label.textContent = `Mode: ${mode}`;
}

function derivePrimaryThreat(details) {
  const chain = details?.hypothesis?.causal_chain;
  const steps = chain?.steps || [];
  const affected = chain?.affected || {};
  const mode = inferEventContextType(details);

  const cpId = affected.chokepoints?.[0];
  const cpObj = cpId ? (twinCache?.chokepoints || []).find((c) => c.id === cpId) : null;
  const topCp = cpObj || (twinCache?.chokepoints || []).slice().sort((a, b) => Number(b.risk_score || 0) - Number(a.risk_score || 0))[0] || null;
  const trigger = steps.find((s) => s.mechanism === "event_trigger") || steps.find((s) => s.evidence_type === "observed") || steps[0];
  const reason = trigger?.claim || details?.hypothesis?.hypothesis_text || "Signal escalation requires monitoring";
  const conf = Number(details?.state?.reconciled_confidence ?? details?.state?.hypothesis_confidence ?? 0);
  const risk = topCp ? (topCp.status === "closed" ? "Critical" : Number(topCp.risk_score || 0) >= 0.7 ? "High" : Number(topCp.risk_score || 0) >= 0.4 ? "Moderate" : "Low") : "Moderate";

  const targetFromClaim = (() => {
    const m = String(reason || "").match(/\bon\s+(.+?)\.\s*Actors:/i);
    return m?.[1]?.trim() || null;
  })();

  const econ = details?.economic?.[0]?.recommendation_payload?.economic_impact;
  const impact = econ?.import_bill_delta_usd_bn != null ? fmtUsdMillionsFromBn(econ.import_bill_delta_usd_bn, { signed: true }) : "Operational watch";

  if (mode === "inventory") {
    return {
      node: targetFromClaim || "Price transmission",
      risk: conf >= 0.7 ? "High" : conf >= 0.45 ? "Moderate" : "Low",
      reason,
      confidence: conf,
      impact,
    };
  }

  return {
    name: topCp?.name || "Primary corridor",
    risk,
    reason,
    confidence: conf,
    impact,
  };
}

function deriveOpsDisruptionLevel(details) {
  const chain = details?.hypothesis?.causal_chain || {};
  const affected = chain.affected || {};
  const criticalRefineries = (details?.refinery || []).filter((r) => String(r?.recommendation_payload?.capacity_status || "").toLowerCase() === "critical").length;
  const routeHits = (affected.routes || []).length;
  const chokeHits = (affected.chokepoints || []).length;
  const mode = inferEventContextType(details);

  if (criticalRefineries > 0 || routeHits >= 2 || chokeHits >= 2) return "High operational disruption";
  if (mode === "inventory" || (routeHits === 0 && chokeHits === 0)) return "Low operational disruption";
  return "Moderate operational disruption";
}

function buildStoryFlow(details) {
  const chain = details?.hypothesis?.causal_chain || {};
  const steps = Array.isArray(chain.steps) ? chain.steps : [];
  const clip = (text, max = 90) => {
    const raw = String(text || "").trim();
    if (raw.length <= max) return raw;
    return `${raw.slice(0, max).replace(/\s+\S*$/, "").trim()}...`;
  };
  const pick = (needle) => steps.find((s) => String(s?.mechanism || "").toLowerCase() === needle) || null;
  const firstObserved = steps.find((s) => String(s?.evidence_type || "").toLowerCase() === "observed") || steps[0] || null;
  const summary = [
    { kicker: "Current situation", step: pick("event_trigger") || firstObserved },
    { kicker: "Transmission", step: pick("throughput_merge") || pick("price_transmission") || steps[1] || firstObserved },
    { kicker: "India exposure", step: pick("india_exposure") || steps.find((s) => String(s?.entity_name || "").toLowerCase().includes("india")) || steps[2] || firstObserved },
    { kicker: "Decision", step: pick("recommendation") || steps[steps.length - 1] || firstObserved },
    { kicker: "Validation", step: { claim: "Run validation replay to verify consistency over recent events." } },
  ];

  return summary.map((item, idx) => ({
    id: idx,
    kicker: item.kicker,
    text: clip(item?.step?.claim || "Awaiting evidence"),
    step: item.step || null,
  }));
}

function setActiveStoryFlow(index) {
  activeStoryFlowIndex = Number.isFinite(index) ? index : -1;
  document.querySelectorAll("#mapStoryFlow .story-flow-step").forEach((btn, i) => {
    btn.classList.toggle("active", i === activeStoryFlowIndex);
    btn.classList.toggle("done", activeStoryFlowIndex >= 0 && i < activeStoryFlowIndex);
  });
}

function renderMapStoryFlow(details) {
  const root = $("mapStoryFlow");
  if (!root) return;
  const flow = buildStoryFlow(details);
  root.innerHTML = flow
    .map((item, i) => `
      <button type="button" class="story-flow-step${i === 0 ? " active" : ""}" data-flow-index="${i}">
        <span class="kicker">${escapeHtml(item.kicker)}</span>
        <span class="text">${escapeHtml(item.text)}</span>
      </button>`)
    .join("");

  setActiveStoryFlow(0);
  root.querySelectorAll(".story-flow-step[data-flow-index]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.flowIndex || -1);
      const node = flow[idx];
      setActiveStoryFlow(idx);
      const geom = resolveStepGeometry(node?.step);
      if (geom?.point && warMap) {
        paintPropagationStep(geom);
        warMap.flyTo({ center: geom.point, zoom: Math.max(4.6, Math.min(6.2, warMap.getZoom())), essential: true, duration: 640 });
      }
      const cap = $("mapStoryCaption");
      if (cap) cap.textContent = `${node?.kicker || "Story"}: ${node?.text || ""}`;
    });
  });
}

function updateEventSpotlight(details) {
  const card = $("eventSpotlight");
  if (!card) return;
  if (!details) {
    card.style.display = "none";
    return;
  }
  const threat = derivePrimaryThreat(details);
  card.style.display = "block";
  $("spotNode").textContent = threat.name;
  $("spotRisk").textContent = threat.risk;
  $("spotReason").textContent = String(threat.reason).slice(0, 110);
  $("spotConf").textContent = fmtPctWhole(threat.confidence);
  $("spotImpact").textContent = threat.impact;
}

async function startEventStoryMode() {
  if (storyModeRunning) return;
  if (!lastPipelineDetails) {
    setStatus("load a pipeline first", "err");
    return;
  }

  const caption = $("mapStoryCaption");
  const btn = $("explainEventBtn");
  storyModeRunning = true;
  if (btn) btn.disabled = true;

  const setCap = (msg) => {
    if (caption) caption.textContent = msg;
  };
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  try {
    const threat = derivePrimaryThreat(lastPipelineDetails);
    setCap(`Observed: ${threat.reason}`);
    focusMapOnEntity(threat.name);
    await wait(1200);

    setCap("Propagation: tracing corridor and route exposure.");
    await playCausalPropagation(lastPipelineDetails?.hypothesis?.causal_chain, { updateCaption: true, perStepMs: 780 });
    await wait(400);

    const refId = lastPipelineDetails?.hypothesis?.causal_chain?.affected?.refineries?.[0];
    const ref = refId ? (twinCache?.refineries || []).find((r) => r.id === refId) : null;
    if (ref?.name) {
      setCap(`India exposure: ${ref.name} and connected assets are under watch.`);
      focusMapOnEntity(ref.name);
      await wait(1300);
    }

    setCap("Recommendation: reviewing procurement and SPR action.");
    flashAndScrollTo("procurementCard");
    await wait(900);

    setCap("Story complete. Use Decision Mode for executive view.");
    clearPropagationOverlay();
    setStatus("event story complete", "ok");
  } catch (err) {
    setStatus(`story mode failed: ${err.message}`, "err");
  } finally {
    clearPropagationOverlay();
    storyModeRunning = false;
    if (btn) btn.disabled = false;
  }
}

function buildWeatherAlerts(twin, marketCtx) {
  const headlines = Array.isArray(marketCtx?.recent_headlines) ? marketCtx.recent_headlines : [];
  const weatherHeadline = headlines.some((h) => /storm|cyclone|typhoon|wind|flood|weather|rain|monsoon/i.test(String(h || "")));
  const alerts = [];

  (twin?.chokepoints || []).forEach((cp) => {
    const risk = Number(cp.risk_score || 0);
    const statusClosed = String(cp.status || "").toLowerCase() === "closed";
    const severity = Math.min(1, risk + (statusClosed ? 0.3 : 0) + (weatherHeadline ? 0.1 : 0));
    if (severity < 0.40) return;
    alerts.push({
      coords: [cp.lon, cp.lat],
      name: cp.name,
      level: severity >= 0.9 ? "high" : "medium",
      severity,
      detail: `Risk ${risk.toFixed(2)} ${statusClosed ? "(closed)" : "(open/restricted)"}${weatherHeadline ? " · weather-linked headline signal detected" : ""}`,
    });
  });

  (twin?.ports || []).forEach((p) => {
    const cong = Number(p.congestion_pct || 0);
    const severity = Math.min(1, cong / 100 + (weatherHeadline ? 0.12 : 0));
    if (severity < 0.45) return;
    alerts.push({
      coords: [p.lon, p.lat],
      name: p.name,
      level: severity >= 0.92 ? "high" : "medium",
      severity,
      detail: `Port congestion ${cong.toFixed(0)}%${weatherHeadline ? " · weather-linked headline signal detected" : ""}`,
    });
  });

  if (!alerts.length) {
    const fallbackCp = (twin?.chokepoints || [])
      .slice()
      .sort((a, b) => Number(b.risk_score || 0) - Number(a.risk_score || 0))[0];
    if (fallbackCp) {
      alerts.push({
        coords: [fallbackCp.lon, fallbackCp.lat],
        name: fallbackCp.name,
        level: "watch",
        severity: Math.max(0.34, Number(fallbackCp.risk_score || 0)),
        detail: `Watch overlay (no severe weather event): chokepoint risk ${Number(fallbackCp.risk_score || 0).toFixed(2)}`,
      });
    }
  }

  return alerts.slice(0, 16);
}

function buildAisCorridorAlerts(twin, tankers) {
  const alerts = [];
  const cps = twin?.chokepoints || [];
  for (const cp of cps) {
    const near = tankers.filter((t) => haversineKm(cp.lat, cp.lon, t.coords[1], t.coords[0]) <= 220);
    if (!near.length) continue;
    const anchored = near.filter((t) => String(t.status || "").toLowerCase() === "anchored").length;
    const anchoredRatio = anchored / near.length;
    const cpRisk = Number(cp.risk_score || 0);
    const severity = Math.min(1, anchoredRatio * 0.75 + cpRisk * 0.45 + Math.min(0.2, near.length / 25));
    if (severity < 0.38 || near.length < 2) continue;
    alerts.push({
      coords: [cp.lon, cp.lat],
      name: cp.name,
      severity,
      level: severity >= 0.82 ? "high" : "medium",
      detail: `${near.length} vessels nearby, anchored ${Math.round(anchoredRatio * 100)}%, chokepoint risk ${cpRisk.toFixed(2)}`,
    });
  }
  if (!alerts.length) {
    const fallback = (twin?.chokepoints || [])
      .map((cp) => {
        const nearCount = tankers.filter((t) => haversineKm(cp.lat, cp.lon, t.coords[1], t.coords[0]) <= 220).length;
        const cpRisk = Number(cp.risk_score || 0);
        return { cp, nearCount, cpRisk, score: nearCount * 0.06 + cpRisk };
      })
      .sort((a, b) => b.score - a.score)[0];
    if (fallback) {
      alerts.push({
        coords: [fallback.cp.lon, fallback.cp.lat],
        name: fallback.cp.name,
        severity: Math.max(0.32, Math.min(0.55, fallback.score)),
        level: "watch",
        detail: `Watch overlay: ${fallback.nearCount} vessels nearby, chokepoint risk ${fallback.cpRisk.toFixed(2)}`,
      });
    }
  }

  return alerts.slice(0, 10);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const toRad = (d) => (d * Math.PI) / 180;
  const R = 6371;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) * Math.sin(dLon / 2);
  return 2 * R * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function buildProcurementArcs(details, twin) {
  const procRec = (details.procurement || [])[0];
  if (!procRec) return [];
  const payload = procRec.recommendation_payload || {};
  const ranking = payload.ranking || [];
  const ports = twin.ports || [];
  const routes = twin.routes || [];

  return ranking
    .filter((row) => row.status === "selected")
    .map((row) => {
      const originPort = ports.find((p) => p.country_iso3.toUpperCase() === row.supplier_country_iso3.toUpperCase());
      const route = routes.find(
        (r) =>
          ports.find((p) => p.id === r.origin_port_id && p.country_iso3.toUpperCase() === row.supplier_country_iso3.toUpperCase()) &&
          ports.find((p) => p.id === r.destination_port_id && p.country_iso3 === "IND")
      );
      let target = null;
      if (route) {
        const dest = ports.find((p) => p.id === route.destination_port_id);
        if (dest) target = [dest.lon, dest.lat];
      } else {
        // Fallback: any Indian port
        const indPort = ports.find((p) => p.country_iso3 === "IND");
        if (indPort) target = [indPort.lon, indPort.lat];
      }
      if (!originPort || !target) return null;
      return {
        source: [originPort.lon, originPort.lat],
        target,
        allocated_kbd: row.allocated_kbd,
        color: [126, 210, 214, 220],
        label: `${row.supplier_country_iso3} → IND (${row.allocated_kbd} kbd)`,
      };
    })
    .filter(Boolean);
}

function getOverlayFocusPoints(layerKey) {
  if (!twinCache) return [];

  if (layerKey === "ais") {
    const sourceTankers = liveAisPoints.length ? liveAisPoints : (twinCache.tankers || []);
    return sourceTankers
      .filter((t) => t && t.lat != null && t.lon != null)
      .slice(0, 160)
      .map((t) => [Number(t.lon), Number(t.lat)]);
  }

  if (layerKey === "weatherAlerts") {
    return buildWeatherAlerts(twinCache, marketCtxCache).map((a) => a.coords);
  }

  if (layerKey === "aisAlerts") {
    const sourceTankers = liveAisPoints.length ? liveAisPoints : (twinCache.tankers || []);
    const tankers = sourceTankers
      .filter((t) => t && t.lat != null && t.lon != null)
      .map((t) => ({ ...t, coords: [Number(t.lon), Number(t.lat)] }));
    return buildAisCorridorAlerts(twinCache, tankers).map((a) => a.coords);
  }

  if (layerKey === "procurement") {
    const arcs = buildProcurementArcs(lastPipelineDetails || {}, twinCache);
    return arcs.flatMap((a) => [a.source, a.target]);
  }

  if (layerKey === "branch") {
    const portById = Object.fromEntries((twinCache.ports || []).map((p) => [p.id, p]));
    return (branchCache?.routes_changed || [])
      .map((rc) => {
        const route = (twinCache.routes || []).find((r) => r.id === rc.id);
        if (!route) return [];
        const o = portById[route.origin_port_id];
        const d = portById[route.destination_port_id];
        const pts = [];
        if (o) pts.push([o.lon, o.lat]);
        if (d) pts.push([d.lon, d.lat]);
        return pts;
      })
      .flat();
  }

  return [];
}

function focusOverlayLayer(layerKey) {
  if (!warMap) return;
  const points = getOverlayFocusPoints(layerKey).filter((p) => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]));
  if (!points.length) return;
  if (points.length === 1) {
    warMap.flyTo({ center: points[0], zoom: Math.max(4.8, warMap.getZoom()), essential: true, duration: 750 });
    return;
  }
  fitBoundsToPoints(points, 90);
}

function updateMapRisk(state, details) {
  if (!warMap) return;

  const disagree = !!state.disagreement;
  const conf = Number(state.reconciled_confidence ?? state.hypothesis_confidence ?? 0);
  let tone = "low";
  if (disagree || conf < 0.45) tone = "high";
  else if (conf < 0.7) tone = "medium";

  const simCount = (details.simulations || []).length;
  const branchTag = branchCache ? ` | branch: ${branchCache.scenario_name}` : "";
  $("mapLegend").textContent = `Risk: ${tone} | confidence: ${fmtPctWhole(conf)} | sims: ${simCount}${branchTag}`;

  lastPipelineDetails = details;
  applyContextAwareMapMode(details);
  updateEventSpotlight(details);
  updateBudgetHint(details);
  refreshDeckLayers();
}

function initMapLayerControls() {
  const box = $("mapLayerControls");
  if (!box) return;
  box.addEventListener("change", (e) => {
    const input = e.target.closest("input[type='checkbox'][data-layer]");
    if (!input) return;
    const key = input.dataset.layer;
    if (!(key in mapLayersState)) return;
    mapLayersState[key] = !!input.checked;
    refreshDeckLayers();
    if (key === "ais" && input.checked) {
      ensureLiveAisPoints(true).then(() => refreshDeckLayers()).catch(() => {});
    }
    const dbg = window.__kavachMapDebug || {};
    const count = key === "weatherAlerts"
      ? dbg.counts?.weatherAlerts
      : key === "aisAlerts"
        ? dbg.counts?.aisAlerts
        : undefined;
    const onOff = input.checked ? "on" : "off";
    setStatus(count != null ? `map layer ${key} ${onOff} · ${count} markers` : `map layer ${key} ${onOff}`, "ok");

    if (input.checked && ["ais", "weatherAlerts", "aisAlerts", "procurement", "branch"].includes(key)) {
      focusOverlayLayer(key);
    }
  });
  syncMapLayerControlsUI();
  renderMapOverlayBadges();
}

// ---------------------------------------------------------------------------
// What-if scenario picker (PRD v2 Upgrade 4)
// ---------------------------------------------------------------------------

let scenarioPresetIndex = {};

async function loadScenarioPresets() {
  try {
    const presets = await apiFetch("/whatif/scenarios");
    scenarioPresetIndex = Object.fromEntries(presets.map((p) => [p.name, p]));
    const sel = $("scenarioSelect");
    sel.innerHTML = '<option value="">— pick a scenario —</option>' +
      presets.map((p) => `<option value="${escapeHtml(p.name)}">${escapeHtml(p.name.replace(/_/g, " "))}</option>`).join("");
  } catch (err) {
    console.warn("failed to load scenario presets", err);
  }
}

function updateScenarioHint() {
  const key = $("scenarioSelect").value;
  const preset = scenarioPresetIndex[key];
  const hintEl = $("scenarioHint");
  if (!preset) {
    hintEl.textContent = "Every scenario runs on a temporary Digital Twin branch. Live state is never modified.";
    return;
  }
  const params = Object.entries(preset.params || {}).map(([k, v]) => `${k}=${v}`).join(", ");
  hintEl.textContent = `${preset.description}${params ? " · defaults: " + params : ""}`;
}

async function runScenarioBranch() {
  const name = $("scenarioSelect").value;
  if (!name) {
    setStatus("pick a scenario preset first", "err");
    return;
  }
  const pipelineId = $("pipelineId").value.trim();
  if (!pipelineId || !lastPipelineDetails?.hypothesis?.id) {
    setStatus("load a pipeline first (need hypothesis_id)", "err");
    return;
  }
  setStatus(`running scenario ${name}...`);
  const missionObjective = inferMissionObjective();
  const result = await apiFetch("/whatif/scenario", {
    method: "POST",
    body: JSON.stringify({
      hypothesis_id: lastPipelineDetails.hypothesis.id,
      scenario_name: name,
      scenario_params: {},
      num_simulations: 2000,
      mission_objective: missionObjective,
    }),
  });

  branchCache = {
    scenario_name: result.scenario_name,
    branch_id: result.branch_id,
    routes_changed: (result.twin_delta?.routes_changed) || [],
    chokepoints_changed: (result.twin_delta?.chokepoints_changed) || [],
    ports_changed: (result.twin_delta?.ports_changed) || [],
  };
  refreshDeckLayers();
  fitMapToBranch(result);

  // Repaint dependent panels from the *branch* — clearly labelled so the
  // demo can compare live vs scenario side-by-side.
  const branchRecommendation = (payload) => [{ simulation_id: null, recommendation_payload: payload || {}, score: 0 }];
  renderRefinery(branchRecommendation(result.refinery));
  renderReplenishment(branchRecommendation(result.policy));
  renderProcurement(branchRecommendation(result.procurement));
  // Re-paint Monte Carlo with the branch overlay + horizontal branch line.
  drawTimeline(mcState.simulations, result, mcState.economic);

  // If the branch procurement carries a causal chain (Upgrade 2), replay it
  // in the reasoning panel so entity attribution reflects the scenario.
  const branchCausal = result.procurement?.causal_chain;
  if (branchCausal) {
    renderCausalChain({
      hypothesis_text: lastPipelineDetails?.hypothesis?.hypothesis_text,
      causal_chain: branchCausal,
      reasoning_chain: result.procurement?.reasoning_chain || [],
    });
  }

  const impact = result.refinery?.refinery_impact?.aggregate || {};
  const lossKbd = impact.throughput_loss_kbd ?? "-";
  const worst = impact.worst_hit_refinery_name || "-";
  $("mapLegend").innerHTML = `Branch <span class="branch-pill">${escapeHtml(result.scenario_name)}</span> | throughput loss: ${lossKbd} kbd | worst: ${escapeHtml(worst)}`;
  renderScenarioOutcome(result);
  setStatus(`scenario ${result.scenario_name} rendered on branch`, "ok");
}

// Structured scenario outcome card — replaces the raw-JSON dump.
function renderScenarioOutcome(result) {
  const box = $("scenarioOutcome");
  if (!box) return;
  if (!result) {
    box.classList.remove("active");
    return;
  }
  $("soName").textContent = result.scenario_name;
  $("soTitle").textContent = result.scenario_description || result.scenario_name;
  $("soSub").textContent = `Branch ${result.branch_id} · parent ${result.parent_branch_id || "live"} · confidence used ${(result.confidence_used || 0).toFixed(2)}`;

  const chips = [];
  const o = result.applied_overrides || {};
  Object.entries(o.chokepoint_status || {}).forEach(([id, status]) =>
    chips.push({ text: `${prettyId(id)}: ${status}`, cls: status === "closed" ? "chip-danger" : "chip-warn" })
  );
  Object.entries(o.port_status || {}).forEach(([id, status]) =>
    chips.push({ text: `${prettyId(id)}: ${status}`, cls: status === "closed" ? "chip-danger" : "chip-warn" })
  );
  Object.entries(o.refinery_status || {}).forEach(([id, status]) =>
    chips.push({ text: `${prettyId(id)}: ${status}`, cls: status === "offline" ? "chip-danger" : "chip-warn" })
  );
  Object.entries(o.route_insurance_multiplier || {}).forEach(([id, mult]) =>
    chips.push({ text: `${prettyId(id)}: insurance ×${Number(mult).toFixed(1)}`, cls: "chip-warn" })
  );
  Object.entries(o.price_shock_pct || {}).forEach(([grade, pct]) =>
    chips.push({ text: `${grade.replace("grade_", "")}: ${pct > 0 ? "+" : ""}${pct}% shock`, cls: pct >= 0 ? "chip-warn" : "chip-ok" })
  );
  (o.supplier_spare_capacity_kbd || []).forEach((cap) =>
    chips.push({ text: `${cap.country_iso3} ${cap.grade_id ? cap.grade_id.replace("grade_", "") : ""}: ${cap.spare_capacity_kbd} kbd`, cls: "chip" })
  );
  $("soChips").innerHTML = chips.length
    ? chips.map((c) => `<span class="chip ${c.cls}">${escapeHtml(c.text)}</span>`).join("")
    : `<span class="chip">no overrides</span>`;

  const ranking = result.procurement?.ranking || [];
  const selected = ranking.filter((r) => r.status === "selected").length;
  const rejected = ranking.filter((r) => r.status === "rejected").length;
  const refill = result.policy?.policy?.replenishment || {};
  const refImpact = result.refinery?.refinery_impact?.aggregate || {};

  const metrics = [
    { label: "Throughput loss", value: refImpact.throughput_loss_kbd != null ? `${Number(refImpact.throughput_loss_kbd).toFixed(0)} kbd` : "-" },
    { label: "Refineries hit", value: refImpact.refinery_count != null ? `${refImpact.refinery_count} (worst: ${refImpact.worst_hit_refinery_name || "-"})` : "-" },
    { label: "Suppliers selected / rejected", value: `${selected} / ${rejected}` },
    {
      label: "Refill plan",
      value: refill.refill_volume_mbbl > 0
        ? `${refill.refill_volume_mbbl} mbbl from ${refill.target_supplier_iso3} @ $${refill.trigger_price_usd_bbl}`
        : "no drawdown required",
    },
  ];
  $("soMetrics").innerHTML = metrics
    .map((m) => `<div class="so-metric"><span>${escapeHtml(m.label)}</span><strong>${escapeHtml(String(m.value))}</strong></div>`)
    .join("");

  $("soRationale").textContent = refill.rationale || "";
  box.classList.add("active");
}

function prettyId(id) {
  if (!id) return "-";
  return String(id).replace(/^(port|cp|ref|spr|route)_/, "").replace(/_/g, " ");
}

function clearScenarioBranch() {
  branchCache = null;
  refreshDeckLayers();
  updateFocusMarker(null);
  $("mapLegend").textContent = "Live twin";
  $("scenarioOutcome")?.classList.remove("active");
  if (lastPipelineDetails) {
    renderRefinery(lastPipelineDetails.refinery || []);
    renderReplenishment(lastPipelineDetails.policy || []);
    drawTimeline(lastPipelineDetails.simulations || [], null, lastPipelineDetails.economic || []);
    renderProcurement(lastPipelineDetails.procurement || []);
    renderCausalChain(lastPipelineDetails.hypothesis);
  }
  setStatus("branch cleared", "ok");
}

// Auto-fit the map to the entities the scenario actually touched — this makes
// the map feel like it's responding to the user's intent instead of sitting
// static in Persian-Gulf-centric view forever.
function fitMapToBranch(result) {
  if (!warMap || !twinCache) return;
  const portById = Object.fromEntries((twinCache.ports || []).map((p) => [p.id, p]));
  const cpById = Object.fromEntries((twinCache.chokepoints || []).map((c) => [c.id, c]));
  const points = [];
  for (const rc of result.twin_delta?.routes_changed || []) {
    const route = (twinCache.routes || []).find((r) => r.id === rc.id);
    if (!route) continue;
    const o = portById[route.origin_port_id];
    const d = portById[route.destination_port_id];
    if (o) points.push([o.lon, o.lat]);
    if (d) points.push([d.lon, d.lat]);
  }
  for (const cc of result.twin_delta?.chokepoints_changed || []) {
    const cp = cpById[cc.id];
    if (cp) points.push([cp.lon, cp.lat]);
  }
  for (const pc of result.twin_delta?.ports_changed || []) {
    const p = portById[pc.id];
    if (p) points.push([p.lon, p.lat]);
  }
  if (points.length >= 2) {
    fitBoundsToPoints(points, 90);
  } else if (points.length === 1) {
    warMap.flyTo({ center: points[0], zoom: 5, essential: true, duration: 900 });
  }
}

function updateIncidentFeed(details) {
  const hyp = details.hypothesis?.hypothesis_text || "No hypothesis text.";
  const rebuttal = buildDynamicRedTeamAssessment(details).text;
  const shortHyp = hyp.length > 120 ? `${hyp.slice(0, 117)}...` : hyp;
  const shortRebuttal = rebuttal.length > 120 ? `${rebuttal.slice(0, 117)}...` : rebuttal;
  $("incidentFeed").textContent = `Primary signal: ${shortHyp} | Red-team note: ${shortRebuttal}`;
}

function finiteNum(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function pickPreferredPolicyRecommendation(policyRecs, simulations) {
  const recs = Array.isArray(policyRecs) ? policyRecs : [];
  if (!recs.length) return null;

  const simById = new Map((Array.isArray(simulations) ? simulations : []).map((s) => [s.id, s]));
  const oneWeek = recs.find((r) => simById.get(r?.simulation_id)?.horizon === "1wk");
  if (oneWeek) return oneWeek;

  return [...recs].sort((a, b) => {
    const da = Number(a?.recommendation_payload?.policy?.recommended_spr_draw_mbd_day1 ?? 0);
    const db = Number(b?.recommendation_payload?.policy?.recommended_spr_draw_mbd_day1 ?? 0);
    return db - da;
  })[0] || recs[0];
}

function buildDynamicRedTeamAssessment(details) {
  const state = details?.state || {};
  const rt = details?.redteam || {};
  const chain = details?.hypothesis?.causal_chain || {};
  const steps = Array.isArray(chain.steps) ? chain.steps : [];
  const eventStep = steps.find((s) => s?.mechanism === "event_trigger")
    || steps.find((s) => s?.evidence_type === "observed")
    || null;
  const eventClaim = eventStep?.claim ? String(eventStep.claim).trim() : "current trigger";

  const sim = (details?.simulations || []).find((s) => s?.horizon === "1wk") || (details?.simulations || [])[0] || null;
  const simP = sim?.percentiles || {};
  const simProb = finiteNum(simP.prob_disruption);
  const simDuration = finiteNum(simP.duration_days);
  const simPriceShock = finiteNum(simP.price_shock_pct);

  const econ = details?.economic?.[0]?.recommendation_payload?.economic_impact || {};
  const importBill = finiteNum(econ.import_bill_delta_usd_bn);
  const cpiDelta = finiteNum(econ.cpi_delta_pct);

  const proc = details?.procurement?.[0]?.recommendation_payload || {};
  const demand = finiteNum(proc.demand_kbd);
  const secured = finiteNum(proc.secured_kbd);
  const explicitGap = finiteNum(proc.gap_kbd);
  const gap = explicitGap != null
    ? explicitGap
    : (demand != null && secured != null ? Math.max(0, demand - secured) : null);

  const preferredPolicyRec = pickPreferredPolicyRecommendation(details?.policy, details?.simulations);
  const policy = preferredPolicyRec?.recommendation_payload?.policy || {};
  const sprDraw = finiteNum(policy.recommended_spr_draw_mbd_day1);

  const refineryImpact = details?.refinery?.[0]?.recommendation_payload?.refinery_impact;
  const refineries = Array.isArray(refineryImpact?.refineries) ? refineryImpact.refineries : [];
  const criticalCount = refineries.filter((r) => r.starved || (finiteNum(r.downtime_probability) || 0) >= 0.45 || (finiteNum(r.expected_utilization_pct) || 0) < 60).length;

  const hyp = finiteNum(state.hypothesis_confidence);
  const rec = finiteNum(state.reconciled_confidence);
  const confidenceDrop = hyp != null && rec != null ? Math.max(0, hyp - rec) : null;
  const derivedCounter = confidenceDrop != null ? Math.max(0.2, Math.min(0.9, 0.35 + confidenceDrop)) : null;
  const counterConfidence = finiteNum(rt.counter_confidence) ?? derivedCounter ?? 0.45;

  const narrative = [];
  narrative.push(`Red-team challenge to ${eventClaim}: base-case impact should be treated as conditional, not automatic.`);

  if (gap != null && demand != null && secured != null) {
    if (gap > 0) {
      narrative.push(`Commercial exposure remains because ${Math.round(gap).toLocaleString()} kbd is still uncovered (${Math.round(secured).toLocaleString()}/${Math.round(demand).toLocaleString()} kbd secured).`);
    } else {
      narrative.push(`Immediate supply pressure is partly absorbed because procurement currently secures ${Math.round(secured).toLocaleString()} of ${Math.round(demand).toLocaleString()} kbd demand.`);
    }
  }

  if (simProb != null) {
    const probText = `${Math.round(simProb * 100)}%`;
    const durText = simDuration != null ? ` over ~${simDuration.toFixed(1)} days` : "";
    narrative.push(`Scenario model places disruption probability at ${probText}${durText}; escalation should wait for stronger operational confirmation.`);
  }

  if (importBill != null || cpiDelta != null || simPriceShock != null) {
    const macro = [];
    if (importBill != null) macro.push(`import bill ${fmtUsdMillionsFromBn(importBill, { signed: true })}`);
    if (cpiDelta != null) macro.push(`CPI +${cpiDelta.toFixed(3)}%`);
    if (simPriceShock != null) macro.push(`price shock +${simPriceShock.toFixed(1)}%`);
    narrative.push(`Business lens: primary risk channel is pricing and macro pass-through (${macro.join(", ")}).`);
  }

  const signals = [];
  if (simProb != null) {
    signals.push(simProb >= 0.6
      ? `Disruption probability is elevated at ${(simProb * 100).toFixed(1)}%; maintain active hedging.`
      : `Disruption probability remains ${(simProb * 100).toFixed(1)}%, below emergency trigger levels.`);
  }
  if (gap != null) {
    signals.push(gap > 0
      ? `Procurement gap still open at ${Math.round(gap).toLocaleString()} kbd.`
      : "Procurement plan currently closes the modeled supply gap.");
  }
  if (sprDraw != null) {
    signals.push(sprDraw > 0
      ? `SPR draw is active at ${sprDraw.toFixed(3)} mbd.`
      : "SPR draw is not triggered under current policy thresholds.");
  }
  signals.push(criticalCount > 0
    ? `${criticalCount} refinery node(s) are in critical stress, so physical outage risk is non-trivial.`
    : "No refinery is in critical stress right now, reducing near-term physical outage risk.");

  const modelSignals = Array.isArray(rt.disproof_signals) ? rt.disproof_signals.filter(Boolean) : [];
  if (signals.length < 4 && modelSignals.length > 0) {
    signals.push(...modelSignals.slice(0, 4 - signals.length));
  }

  const text = narrative.join(" ") || (rt.rebuttal_text || "No rebuttal available.");
  const impact = confidenceDrop != null
    ? `Impact: confidence reduced from ${(hyp * 100).toFixed(1)}% to ${(rec * 100).toFixed(1)}% after counter-evidence.`
    : "Impact: confidence adjusted after red-team challenge.";

  return {
    text,
    disproofSignals: signals,
    counterConfidence,
    impact,
  };
}

function drawTimeline(simulations, branchScenario, economic) {
  renderMonteCarlo(simulations, branchScenario, economic);
}

// ---------------------------------------------------------------------------
// Monte Carlo panel — KPI strip + fan chart + distribution histogram
// ---------------------------------------------------------------------------

const HORIZONS = ["24h", "72h", "1wk", "1mo"];
const HORIZON_DAYS = { "24h": 1, "72h": 3, "1wk": 7, "1mo": 30 };

const mcState = {
  simulations: [],
  branchScenario: null,
  horizon: "1wk",
  metric: "price", // "price" | "duration"
  economic: [],   // live economic recommendations, one per simulation
};

function renderMonteCarlo(simulations, branchScenario, economic) {
  mcState.simulations = Array.isArray(simulations) ? simulations : [];
  mcState.branchScenario = branchScenario || null;
  if (Array.isArray(economic)) mcState.economic = economic;

  const tag = $("mcBranchTag");
  if (mcState.branchScenario) {
    tag.textContent = mcState.branchScenario.scenario_name || "branch";
    tag.style.display = "inline-block";
  } else {
    tag.style.display = "none";
  }

  const activeSim = pickHorizon(mcState.simulations, mcState.horizon);
  const branchPct = mcState.branchScenario?.scenario_percentiles || null;

  renderMcKpis(activeSim, branchPct);
  drawFanChart($("mcFanChart"), mcState.simulations, mcState.metric, mcState.branchScenario);
  drawHistogram($("mcHistogram"), activeSim, mcState.metric, mcState.branchScenario);
  updateMcHistTitle();
  renderMcLegend(activeSim, branchPct);
}

function pickHorizon(sims, horizon) {
  return sims.find((s) => s.horizon === horizon) || sims[0] || null;
}

function fmtPct(v, digits = 1) {
  if (v == null || isNaN(v)) return "-";
  return `${(v * 100).toFixed(digits)}%`;
}

function fmtPctWhole(v) {
  if (v == null || isNaN(v)) return "-";
  return `${Math.round(Number(v) * 100)}%`;
}

function fmtDurationHuman(days) {
  if (days == null || isNaN(days)) return "-";
  const d = Number(days);
  if (d < 1) return `${Math.max(1, Math.round(d * 24))} hours`;
  return `${Math.round(d)} days`;
}

function fmtUsdBn(v) {
  if (v == null || isNaN(v)) return "-";
  return fmtUsdMillionsFromBn(v, { signed: true });
}

function fmtUsdMillionsFromBn(v, opts = {}) {
  if (v == null || isNaN(v)) return "-";
  const n = Number(v);
  const m = Math.abs(n * 1000);
  const digits = m >= 100 ? 0 : m >= 10 ? 1 : 2;
  const value = m.toFixed(digits);
  const sign = n < 0 ? "-" : (opts.signed ? "+" : "");
  const approx = opts.approx ? "~" : "";
  return `${approx}${sign}$${value}M`;
}

function fmtNum(v, digits = 2, suffix = "") {
  if (v == null || isNaN(v)) return "-";
  return `${Number(v).toFixed(digits)}${suffix}`;
}

function renderMcKpis(sim, branchPct) {
  if (!sim) {
    ["mcProb", "mcPriceP50", "mcPriceBand", "mcDurP50", "mcImportDelta"].forEach((id) => ($(id).textContent = "-"));
    ["mcMostLikely", "mcRange", "mcExpectedDuration"].forEach((id) => ($(id).textContent = "-"));
    return;
  }
  const p = sim.percentiles || {};
  const priceP50 = p.price_shock_pct;
  const priceP10 = p.p10_price_shock_pct;
  const priceP90 = p.p90_price_shock_pct;
  const durP50 = p.duration_days;
  const durP10 = p.p10_duration_days;
  const durP90 = p.p90_duration_days;
  const prob = p.disruption_prob;

  const branchBadge = (liveVal, branchVal, formatter) => {
    if (branchVal == null || liveVal == null) return "";
    const delta = branchVal - liveVal;
    if (Math.abs(delta) < 1e-6) return "";
    const arrow = delta > 0 ? "↑" : "↓";
    const cls = delta > 0 ? "constraint-warn" : "constraint-ok";
    return ` <span class="${cls}" style="font-size:.75rem;">${arrow} ${formatter(branchVal)}</span>`;
  };

  $("mcProb").innerHTML = `${fmtPct(prob, 1)}${branchBadge(prob, branchPct?.disruption_prob, (v) => fmtPct(v, 1))}`;
  $("mcPriceP50").innerHTML = `+${fmtPct(priceP50, 1)}${branchBadge(priceP50, branchPct?.price_shock_pct, (v) => "+" + fmtPct(v, 1))}`;
  $("mcPriceBand").textContent = priceP10 != null && priceP90 != null
    ? `[${fmtPct(priceP10, 1)} … ${fmtPct(priceP90, 1)}]`
    : "-";
  $("mcDurP50").innerHTML = `${fmtNum(durP50, 1, "d")}${branchBadge(durP50, branchPct?.duration_days, (v) => fmtNum(v, 1, "d"))}`;

  // Import bill delta — pulled from the real economic recommendation for
  // this simulation (agents.economic_agent). No hardcoded scaling constants.
  const activeEcon = (mcState.economic || []).find((e) => e.simulation_id === sim.id);
  const liveDelta = activeEcon?.recommendation_payload?.economic_impact?.import_bill_delta_usd_bn;
  const branchDelta = mcState.branchScenario?.procurement?.context?.import_bill_delta_usd_bn
    ?? mcState.branchScenario?.policy?.drivers?.import_bill_delta_usd_bn
    ?? null;
  if (liveDelta != null) {
    $("mcImportDelta").innerHTML =
      `${fmtUsdMillionsFromBn(liveDelta, { signed: true })}${branchBadge(liveDelta, branchDelta, (v) => fmtUsdMillionsFromBn(v, { signed: true }))}`;
  } else {
    $("mcImportDelta").textContent = "-";
  }

  $("mcMostLikely").textContent = `+${fmtPctWhole(priceP50)}`;
  $("mcRange").textContent = (priceP10 != null && priceP90 != null)
    ? `${fmtPctWhole(priceP10)} - ${fmtPctWhole(priceP90)}`
    : "-";
  $("mcExpectedDuration").textContent = fmtDurationHuman(durP50);
}

function updateMcHistTitle() {
  const metricLabel = mcState.metric === "price" ? "price shock" : "duration";
  $("mcHistTitle").textContent = `Distribution · ${mcState.horizon} · ${metricLabel}`;
}

function renderMcLegend(activeSim, branchPct) {
  if (!activeSim) {
    $("timelineLegend").textContent = "Trigger a pipeline to populate simulations.";
    return;
  }
  const meta = activeSim.metadata || {};
  const parts = [
    `n=${meta.num_simulations ?? "?"}`,
    `horizon_days=${meta.horizon_days ?? HORIZON_DAYS[activeSim.horizon] ?? "?"}`,
    `conf=${meta.hypothesis_confidence ?? "?"}`,
  ];
  if (branchPct) parts.push(`branch overlay active`);
  $("timelineLegend").textContent = parts.join(" · ");
}

// -- Fan chart ---------------------------------------------------------------

function drawFanChart(canvas, sims, metric, branchScenario) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const padL = 44;
  const padR = 12;
  const padT = 14;
  const padB = 26;
  const w = canvas.width - padL - padR;
  const h = canvas.height - padT - padB;

  const ordered = HORIZONS.map((h) => sims.find((s) => s.horizon === h)).filter(Boolean);
  if (!ordered.length) {
    ctx.fillStyle = "#51675f";
    ctx.font = "12px 'IBM Plex Sans', sans-serif";
    ctx.fillText("No simulations loaded", padL + 6, padT + 20);
    return;
  }

  const isPrice = metric === "price";
  const keyP10 = isPrice ? "p10_price_shock_pct" : "p10_duration_days";
  const keyP50 = isPrice ? "price_shock_pct" : "duration_days";
  const keyP90 = isPrice ? "p90_price_shock_pct" : "p90_duration_days";

  const values = ordered.flatMap((s) => [s.percentiles?.[keyP10], s.percentiles?.[keyP50], s.percentiles?.[keyP90]]).filter((v) => v != null);
  const branchVal = branchScenario?.scenario_percentiles?.[keyP50] ?? null;
  if (branchVal != null) values.push(branchVal);
  const yMin = 0;
  const yMax = Math.max(0.1, Math.max(...values) * 1.15);

  // Axes
  ctx.strokeStyle = "#17272f";
  ctx.lineWidth = 1;
  ctx.strokeRect(padL, padT, w, h);
  // Y ticks (4)
  ctx.fillStyle = "#51675f";
  ctx.font = "10px 'IBM Plex Sans', sans-serif";
  for (let i = 0; i <= 4; i++) {
    const v = yMin + (yMax - yMin) * (i / 4);
    const y = padT + h - (i / 4) * h;
    ctx.beginPath();
    ctx.moveTo(padL - 4, y);
    ctx.lineTo(padL, y);
    ctx.stroke();
    const label = isPrice ? `${(v * 100).toFixed(0)}%` : `${v.toFixed(0)}d`;
    ctx.textAlign = "right";
    ctx.fillText(label, padL - 6, y + 3);
    // Faint gridline
    ctx.strokeStyle = "rgba(30,47,56,0.55)";
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + w, y);
    ctx.stroke();
    ctx.strokeStyle = "#17272f";
  }
  ctx.textAlign = "start";

  const xForIdx = (i) => padL + (ordered.length === 1 ? w / 2 : (i / (ordered.length - 1)) * w);
  const yForVal = (v) => padT + h - ((v - yMin) / (yMax - yMin || 1)) * h;

  // Build fan band from p10 forward and p90 backward
  ctx.beginPath();
  ordered.forEach((s, i) => {
    const v = s.percentiles?.[keyP10] ?? 0;
    const x = xForIdx(i);
    const y = yForVal(v);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  for (let i = ordered.length - 1; i >= 0; i--) {
    const v = ordered[i].percentiles?.[keyP90] ?? 0;
    ctx.lineTo(xForIdx(i), yForVal(v));
  }
  ctx.closePath();
  ctx.fillStyle = "rgba(20,184,166,0.18)";
  ctx.strokeStyle = "rgba(20,184,166,0.55)";
  ctx.lineWidth = 1;
  ctx.fill();
  ctx.stroke();

  // p50 line
  ctx.beginPath();
  ordered.forEach((s, i) => {
    const x = xForIdx(i);
    const y = yForVal(s.percentiles?.[keyP50] ?? 0);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = "#22c55e";
  ctx.lineWidth = 2;
  ctx.stroke();

  // p50 dots + labels
  ordered.forEach((s, i) => {
    const x = xForIdx(i);
    const val = s.percentiles?.[keyP50];
    const y = yForVal(val ?? 0);
    ctx.fillStyle = "#22c55e";
    ctx.beginPath();
    ctx.arc(x, y, 3.2, 0, Math.PI * 2);
    ctx.fill();
    // horizon label on X axis
    ctx.fillStyle = "#86a8b1";
    ctx.textAlign = "center";
    ctx.fillText(s.horizon, x, padT + h + 16);
  });
  ctx.textAlign = "start";

  // Branch overlay: horizontal band at branch value across full width
  if (branchScenario?.scenario_percentiles) {
    const bP50 = branchScenario.scenario_percentiles[keyP50];
    if (bP50 != null) {
      const y = yForVal(bP50);
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = "#ff8fa1";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + w, y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#ff8fa1";
      ctx.textAlign = "right";
      const label = isPrice ? `branch p50 ${(bP50 * 100).toFixed(1)}%` : `branch p50 ${bP50.toFixed(1)}d`;
      ctx.fillText(label, padL + w - 4, y - 4);
      ctx.textAlign = "start";
    }
  }
}

// -- Histogram ---------------------------------------------------------------

function drawHistogram(canvas, sim, metric, branchScenario) {
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const padL = 44;
  const padR = 12;
  const padT = 14;
  const padB = 26;
  const w = canvas.width - padL - padR;
  const h = canvas.height - padT - padB;

  if (!sim) {
    ctx.fillStyle = "#51675f";
    ctx.font = "12px 'IBM Plex Sans', sans-serif";
    ctx.fillText("No simulation for this horizon", padL + 6, padT + 20);
    return;
  }

  const isPrice = metric === "price";
  const samplesKey = isPrice ? "price_shock_pct_samples" : "duration_days_samples";
  const samples = sim.distribution?.[samplesKey] || [];
  if (!samples.length) {
    ctx.fillStyle = "#51675f";
    ctx.font = "12px 'IBM Plex Sans', sans-serif";
    ctx.fillText("No distribution samples available", padL + 6, padT + 20);
    return;
  }

  const p = sim.percentiles || {};
  const p10 = p[isPrice ? "p10_price_shock_pct" : "p10_duration_days"];
  const p50 = p[isPrice ? "price_shock_pct" : "duration_days"];
  const p90 = p[isPrice ? "p90_price_shock_pct" : "p90_duration_days"];

  const branchP50 = branchScenario?.scenario_percentiles?.[isPrice ? "price_shock_pct" : "duration_days"];

  const min = Math.min(...samples);
  const max = Math.max(...samples);
  const range = Math.max(max - min, 1e-6);
  const bins = 24;
  const counts = new Array(bins).fill(0);
  samples.forEach((v) => {
    const idx = Math.min(bins - 1, Math.floor(((v - min) / range) * bins));
    counts[idx] += 1;
  });
  const maxCount = Math.max(...counts, 1);

  // Axes
  ctx.strokeStyle = "#17272f";
  ctx.strokeRect(padL, padT, w, h);
  // Y label ticks
  ctx.fillStyle = "#51675f";
  ctx.font = "10px 'IBM Plex Sans', sans-serif";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const c = Math.round((maxCount * i) / 4);
    const y = padT + h - (i / 4) * h;
    ctx.fillText(String(c), padL - 6, y + 3);
    ctx.strokeStyle = "rgba(30,47,56,0.55)";
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + w, y);
    ctx.stroke();
    ctx.strokeStyle = "#17272f";
  }
  ctx.textAlign = "start";

  const binW = w / bins;
  counts.forEach((c, i) => {
    const bh = (c / maxCount) * h;
    const x = padL + i * binW;
    const y = padT + h - bh;
    ctx.fillStyle = "rgba(20,184,166,0.65)";
    ctx.fillRect(x + 1, y, binW - 2, bh);
  });

  // X ticks (min, p50, max)
  const fmt = (v) => (isPrice ? `${(v * 100).toFixed(0)}%` : `${v.toFixed(0)}d`);
  ctx.fillStyle = "#86a8b1";
  ctx.textAlign = "center";
  ctx.fillText(fmt(min), padL, padT + h + 16);
  ctx.fillText(fmt(max), padL + w, padT + h + 16);
  ctx.textAlign = "start";

  // Percentile markers
  const drawMarker = (val, color, label) => {
    if (val == null) return;
    const clamped = Math.max(min, Math.min(max, val));
    const x = padL + ((clamped - min) / range) * w;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, padT + h);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = "10px 'IBM Plex Sans', sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(`${label} ${fmt(val)}`, x, padT - 3);
    ctx.textAlign = "start";
  };

  drawMarker(p10, "#f59e0b", "p10");
  drawMarker(p50, "#22c55e", "p50");
  drawMarker(p90, "#f43f5e", "p90");
  drawMarker(branchP50, "#ff8fa1", "branch");
}

// Toolbar wiring
function bindMonteCarloToolbar() {
  $("mcHorizonPills").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-h]");
    if (!btn) return;
    mcState.horizon = btn.dataset.h;
    $("mcHorizonPills").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    renderMonteCarlo(mcState.simulations, mcState.branchScenario);
  });
  $("mcMetricPills").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-metric]");
    if (!btn) return;
    mcState.metric = btn.dataset.metric;
    $("mcMetricPills").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
    renderMonteCarlo(mcState.simulations, mcState.branchScenario);
  });
}

function renderProcurement(recs) {
  const el = $("procList");
  const exec = $("procExecSummary");
  if (!recs.length) {
    $("procDecisionScore").textContent = "-";
    $("procSelectedCount").textContent = "-";
    $("procRejectedCount").textContent = "-";
    $("outSavings").textContent = "-";
    $("outContinuity").textContent = "-";
    $("outCoverage").textContent = "-";
    $("opExpectedEffect").textContent = "-";
    $("opMonitor").textContent = "-";
    $("opTrigger").textContent = "-";
    $("opReplan").textContent = "-";
    $("rtRule").textContent = "-";
    $("rtEvidence").textContent = "-";
    $("rtSimulation").textContent = "-";
    $("rtOptimization").textContent = "-";
    $("rtDecision").textContent = "-";
    if (exec) exec.textContent = "No procurement action is recommended yet.";
    el.innerHTML = "<p style='color:var(--muted);'>No procurement data loaded.</p>";
    return;
  }

  // Use the best-scoring plan (first item); show a note if multiple exist.
  const best = recs[0];
  const p = best.recommendation_payload || {};
  const proc = p.procurement || {};
  const ranking = p.ranking || [];

  const sourceLabel = (s) => {
    if (!s || s === "twin" || s === "digital_twin") return "Digital Twin";
    return String(s).replace(/_/g, " ");
  };

  const demandKbd = Number(proc.demand_kbd || 0);
  const securedKbd = Number(proc.secured_kbd || 0);
  const coverPct = demandKbd > 0 ? Math.min(100, Math.round((securedKbd / demandKbd) * 100)) : 0;
  const coverColor = coverPct >= 90 ? "#22c55e" : coverPct >= 70 ? "#f59e0b" : "#f43f5e";
  const preferredPolicyRec = pickPreferredPolicyRecommendation(lastPipelineDetails?.policy, lastPipelineDetails?.simulations);
  const savings = Number(preferredPolicyRec?.recommendation_payload?.policy?.replenishment?.estimated_savings_vs_spot_usd_bn ?? NaN);
  $("outSavings").textContent = Number.isFinite(savings) && savings > 0 ? fmtUsdMillionsFromBn(savings, { approx: true }) : "Cost stability";
  $("outContinuity").textContent = `${Math.max(60, coverPct)}%`;
  $("outCoverage").textContent = coverPct >= 90 ? "Coverage maintained" : coverPct >= 75 ? "Coverage watchlist" : "Coverage at risk";

  const selected = ranking.filter((r) => r.status === "selected");
  const rejected = ranking.filter((r) => r.status === "rejected");
  const topSelected = selected[0] || null;
  $("procSelectedCount").textContent = String(selected.length);
  $("procRejectedCount").textContent = String(rejected.length);

  // Transparent multi-criteria decision score for judge explainability.
  const confScore = Math.max(0, Math.min(100, Math.round((Number(topSelected?.confidence) || 0) * 100)));
  const constraints = topSelected?.constraints || [];
  const riskScore = constraints.length
    ? Math.round((constraints.filter((c) => c.satisfied).length / constraints.length) * 100)
    : 70;
  const blendConstraints = constraints.filter((c) => /blend|quality|compat/i.test(String(c.name || c.constraint || "")));
  const transitConstraints = constraints.filter((c) => /transit|shipping|route|logistics|port/i.test(String(c.name || c.constraint || "")));
  const blendScore = blendConstraints.length
    ? Math.round((blendConstraints.filter((c) => c.satisfied).length / blendConstraints.length) * 100)
    : riskScore;
  const transitScore = transitConstraints.length
    ? Math.round((transitConstraints.filter((c) => c.satisfied).length / transitConstraints.length) * 100)
    : riskScore;
  const costScore = confScore || 70;
  const decisionScore = Math.round((0.40 * costScore) + (0.35 * riskScore) + (0.15 * blendScore) + (0.10 * transitScore));
  $("procDecisionScore").textContent = `${decisionScore}/100`;
  if (exec) {
    const topCountry = topSelected?.supplier_country_name || topSelected?.supplier_country_iso3 || "best-fit supplier";
    const topAlloc = topSelected?.allocated_kbd != null ? `${Number(topSelected.allocated_kbd).toLocaleString()} kbd` : "target allocation";
    const savingsText = Number.isFinite(savings) && savings > 0
      ? `${fmtUsdMillionsFromBn(savings, { approx: true })} expected savings`
      : "cost stability expected";
    exec.textContent = `Recommended action: prioritize ${topCountry} at ${topAlloc}. Coverage outlook ${coverPct}% with ${savingsText}.`;
  }

  // Step-2 Addendum: Recommendation -> Expected Effect -> Monitor -> Trigger -> Replan.
  const riskScoreNow = Number(lastPipelineDetails?.state?.reconciled_confidence ?? 0);
  const triggerThreshold = 0.75;
  const triggerLabel = `Corridor risk > ${triggerThreshold.toFixed(2)}`;
  const expectedEffect = coverPct >= 90
    ? `Maintain ${securedKbd.toLocaleString()} kbd coverage`
    : `Stabilize to ${coverPct}% coverage`;
  const monitorLabel = "Chokepoint risk + AIS anomaly + congestion";
  const replanLabel = riskScoreNow >= triggerThreshold
    ? "Trigger immediate re-optimization"
    : "Auto-rerun optimization if trigger breached";

  $("opExpectedEffect").textContent = expectedEffect;
  $("opMonitor").textContent = monitorLabel;
  $("opTrigger").textContent = triggerLabel;
  $("opReplan").textContent = replanLabel;

  // Step-5 Addendum: explicit recommendation trace for reproducibility.
  const ruleText = `Weighted score: cost 40%, risk 35%, blend 15%, transit 10% (score ${decisionScore}/100)`;
  const evidenceText = `${sourceLabel(proc.universe_source)} + ${constraints.filter((c) => c.satisfied).length}/${constraints.length || 0} constraints satisfied`;
  const simulationText = recs.length > 1
    ? `${recs.length} simulation outputs evaluated; selected best scoring plan`
    : `Single scenario recommendation (simulation context ${best.simulation_id ?? "n/a"})`;
  const optimizationText = `${securedKbd.toLocaleString()} / ${demandKbd.toLocaleString()} kbd secured across ${selected.length} selected suppliers`;
  const decisionText = topSelected?.why_ranked
    ? topSelected.why_ranked
    : selected.length
      ? `Top-ranked supplier ${topSelected?.supplier_country_iso3 || "n/a"} chosen for feasibility and coverage`
      : "No supplier selected; maintain current procurement posture";

  $("rtRule").textContent = ruleText;
  $("rtEvidence").textContent = evidenceText;
  $("rtSimulation").textContent = simulationText;
  $("rtOptimization").textContent = optimizationText;
  $("rtDecision").textContent = decisionText;

  // Coverage bar header
  let html = `
    <div style="margin-bottom:12px;">
      <div style="display:flex; justify-content:space-between; font-size:.82rem; color:var(--muted); margin-bottom:4px;">
        <span>Import demand coverage</span>
        <span style="color:${coverColor}; font-weight:600;">${securedKbd.toLocaleString()} / ${demandKbd.toLocaleString()} kbd</span>
      </div>
      <div style="background:#0f2028; border-radius:4px; height:8px; overflow:hidden;">
        <div style="width:${coverPct}%; height:100%; background:${coverColor}; border-radius:4px; transition:width .4s;"></div>
      </div>
      <div style="display:flex; justify-content:space-between; font-size:.75rem; color:var(--muted); margin-top:3px;">
        <span>Source: ${escapeHtml(sourceLabel(proc.universe_source))}</span>
        <span>${selected.length} suppliers selected &nbsp;·&nbsp; ${rejected.length} rejected</span>
      </div>
    </div>`;

  if (!ranking.length) {
    html += `<p style="color:var(--muted); font-size:.85rem; font-style:italic;">No supplier evaluation data available for this scenario — the current event may not require new procurement actions.</p>`;
  } else {
    const renderRow = (row) => {
      const isSelected = row.status === "selected";
      const grade = row.grade_name || (row.grade_id ? row.grade_id.replace("grade_", "").toUpperCase() : "—");
      const country = row.supplier_country_name || row.supplier_country_iso3 || "—";
      const confPct = Math.round((Number(row.confidence) || 0) * 100);
      const allocKbd = row.allocated_kbd != null ? `${Number(row.allocated_kbd).toLocaleString()} kbd` : "—";
      const constraints = (row.constraints || []);
      const passCount = constraints.filter((c) => c.satisfied).length;
      const failCount = constraints.length - passCount;
      const constraintSummary = constraints.length
        ? `<span class="constraint-ok" title="Passed checks">${passCount} ✓</span>${failCount ? ` <span class="constraint-warn" title="Failed checks">${failCount} ✗</span>` : ""}`
        : "";
      const rej = (row.rejected_reasons || []).join(" · ");
      const borderColor = isSelected ? "#1e3d2a" : "#3d1e25";
      const statusChip = isSelected
        ? `<span class="constraint-ok" style="padding:2px 7px; border-radius:999px; font-size:.72rem;">SELECTED</span>`
        : `<span class="constraint-warn" style="padding:2px 7px; border-radius:999px; font-size:.72rem;">REJECTED</span>`;
      return `
        <div style="border:1px solid ${borderColor}; border-radius:8px; padding:9px 11px; margin-bottom:7px;">
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:6px;">
            <div style="font-weight:600; font-size:.92rem;">${escapeHtml(country)}<span style="font-weight:400; color:var(--muted); margin-left:8px; font-size:.82rem;">${escapeHtml(grade)}</span></div>
            <div style="display:flex; gap:8px; align-items:center;">${statusChip}${constraintSummary}</div>
          </div>
          <div style="display:flex; gap:16px; flex-wrap:wrap; font-size:.82rem; color:var(--muted); margin-top:5px;">
            <span>Allocated: <strong style="color:var(--accent);">${escapeHtml(allocKbd)}</strong></span>
            <span>Confidence: <strong>${confPct}%</strong></span>
            ${row.why_ranked ? `<span style="color:var(--muted);">${escapeHtml(row.why_ranked)}</span>` : ""}
          </div>
          ${rej ? `<div style="color:var(--danger); font-size:.78rem; margin-top:4px;">${escapeHtml(rej)}</div>` : ""}
        </div>`;
    };

    const selectedRows = selected.slice(0, 3).map(renderRow).join("");
    const selectedOverflowRows = selected.slice(3, 8).map(renderRow).join("");
    const rejectedRows = rejected.slice(0, 8).map(renderRow).join("");

    html += `<h3 style="margin:8px 0 6px;">Selected suppliers</h3>`;
    html += selectedRows || `<p style="color:var(--muted); font-size:.82rem;">No selected suppliers for this scenario.</p>`;
    html += `
      <details class="panel-details" style="margin-top:6px;">
        <summary>Why this recommendation?</summary>
        <div style="font-size:.83rem; color:var(--muted); margin-top:6px;">Top supplier: <strong style="color:var(--ink);">${escapeHtml(topSelected?.supplier_country_name || topSelected?.supplier_country_iso3 || "n/a")}</strong></div>
        <div style="margin-top:8px; display:grid; gap:4px; font-size:.82rem; color:var(--muted);">
          <div>Cost (40%): <strong>${costScore}/100</strong></div>
          <div>Risk (35%): <strong>${riskScore}/100</strong></div>
          <div>Blend (15%): <strong>${blendScore}/100</strong></div>
          <div>Transit (10%): <strong>${transitScore}/100</strong></div>
        </div>
      </details>`;
    if (selected.length > 3) {
      html += `<details class="panel-details" style="margin-top:6px;"><summary>Show more selected suppliers (${selected.length - 3})</summary>${selectedOverflowRows}</details>`;
    }
    if (rejected.length) {
      html += `<details class="panel-details" style="margin-top:6px;"><summary>Show rejected suppliers (${rejected.length})</summary>${rejectedRows}</details>`;
    }
  }

  if (recs.length > 1) {
    html += `<p style="font-size:.75rem; color:var(--muted); margin-top:6px;">Best plan shown · ${recs.length} simulation runs evaluated.</p>`;
  }

  el.innerHTML = html;
}

// ---------------------------------------------------------------------------
// Live Market Context ticker
// ---------------------------------------------------------------------------

async function loadMarketTicker() {
  const el = $("marketTicker");
  if (!el) return;
  try {
    const [ctx, recentLive] = await Promise.all([
      apiFetch("/signals/market-context"),
      apiFetch("/signals/recent-live?limit=12").catch(() => []),
    ]);
    marketCtxCache = ctx;
    const stats = [];
    if (ctx.brent_usd != null) {
      stats.push(`<div class="ticker-item"><span class="ticker-label">Brent</span><span class="ticker-value up">$${ctx.brent_usd}/bbl</span></div>`);
    }
    if (ctx.wti_usd != null) {
      stats.push(`<div class="ticker-item"><span class="ticker-label">WTI</span><span class="ticker-value up">$${ctx.wti_usd}/bbl</span></div>`);
    }
    if (ctx.eia_note) {
      stats.push(`<div class="ticker-item"><span class="ticker-label">EIA/FRED</span><span class="ticker-value neutral">${escapeHtml(ctx.eia_note)}</span></div>`);
    }
    if (ctx.signal_count) {
      stats.push(`<div class="ticker-item"><span class="ticker-label">Signals</span><span class="ticker-value neutral">${ctx.signal_count.toLocaleString()}</span></div>`);
    }
    const marketTs = ctx.price_last_update_utc || ctx.latest_signal_utc || null;
    if (marketTs) {
      const parsed = parseIsoMs(marketTs);
      let marketNote = "";
      if (Number.isFinite(parsed)) {
        const ageSec = Math.max(0, Math.round((Date.now() - parsed) / 1000));
        const nextSec = Math.max(0, (24 * 3600) - ageSec);
        if (ageSec <= (36 * 3600)) {
          marketNote = `Daily feed · Next update ~${formatEtaCompact(nextSec)}`;
        } else {
          marketNote = "Daily feed · Update delayed";
        }
      } else {
        marketNote = "Daily feed";
      }
      stats.push(`<div class="ticker-item"><span class="ticker-label">Market snapshot</span><span class="ticker-value neutral">${escapeHtml(formatAge(marketTs))}</span><span class="ticker-note">${escapeHtml(marketNote)}</span></div>`);
    }

    const fallback = "Monitoring: no high-signal energy or supply-chain headline in latest feed";
    const includeRe = /(\bsupply\s*chain\b|\benergy\b|\boil\b|\bgas\b|\blng\b|\bbrent\b|\bwti\b|\bshipping\b|\bfreight\b|\btanker\b|\bais\b|\bport\b|\bchokepoint\b|\bhormuz\b|\bsuez\b|\bbab[-\s]?el[-\s]?mandeb\b|\bstrait\b|\bopec\b|\bsanction\w*\b|\bofac\b|\bembargo\b|\brefiner\w*\b|\binventory\b|\beia\b|\bfred\b|\bdiesel\b|\bcrude\b|\bterminal\b|\bpipeline\b|\bexport\b|\bimport\b|\biran\b|\battack\b|\bstrike\b|\bconflict\b)/i;
    const excludeRe = /(fifa|football|soccer|cricket|nba|tennis|movie|cinema|celebrity|gossip|fashion|music\s+awards|reality\s+show|tv\s+show)/i;

    const detailed = Array.isArray(ctx.recent_headlines_detailed) ? ctx.recent_headlines_detailed : [];
    const legacy = Array.isArray(ctx.recent_headlines) ? ctx.recent_headlines : [];
    const records = [];

    const sourceLabel = (src) => {
      const key = String(src || "").toLowerCase();
      if (key.includes("guardian")) return "Guardian";
      if (key.includes("newsapi")) return "NewsAPI";
      if (key.includes("gdelt")) return "GDELT";
      if (key.includes("ais")) return "AIS";
      if (key.includes("event_graph")) return "Event";
      if (key.includes("eia")) return "EIA";
      if (key.includes("fred")) return "FRED";
      return key ? titleize(key) : "Feed";
    };

    const sourceKind = (src) => {
      const key = String(src || "").toLowerCase();
      if (key.includes("ais")) return { cls: "ais", label: "AIS" };
      if (key.includes("eia") || key.includes("fred") || key.includes("alpha")) return { cls: "market", label: "Market" };
      if (key.includes("event_graph")) return { cls: "system", label: "System" };
      return { cls: "news", label: "News" };
    };

    for (const item of detailed) {
      const title = String(item?.title || "").trim();
      if (!title) continue;
      records.push({
        title,
        url: typeof item?.url === "string" && item.url.trim() ? item.url.trim() : null,
        source: String(item?.source || "").trim(),
      });
    }
    for (const titleRaw of legacy) {
      const title = String(titleRaw || "").trim();
      if (!title) continue;
      records.push({ title, url: null, source: "legacy_feed" });
    }

    const liveItems = Array.isArray(recentLive) ? recentLive : [];
    for (const item of liveItems) {
      const action = String(item?.action_type || "signal").replace(/_/g, " ").trim();
      const target = String(item?.target || "energy market").replace(/_/g, " ").trim();
      if (!target) continue;
      const line = `${action}: ${target}`;
      records.push({ title: line, url: null, source: "event_graph" });
    }

    const seen = new Set();
    const filtered = [];
    for (const rec of records) {
      const key = rec.title.toLowerCase().replace(/\s+/g, " ").trim();
      if (!key || seen.has(key)) continue;
      seen.add(key);
      if (!includeRe.test(rec.title)) continue;
      if (excludeRe.test(rec.title)) continue;
      filtered.push(rec);
    }

    const sourceBuckets = new Map();
    for (const rec of filtered) {
      const key = String(rec.source || "other").toLowerCase() || "other";
      if (!sourceBuckets.has(key)) sourceBuckets.set(key, []);
      sourceBuckets.get(key).push(rec);
    }

    const bucketOrder = Array.from(sourceBuckets.keys()).sort((a, b) => {
      const pa = a === "event_graph" ? 0 : a.includes("guardian") ? 1 : a.includes("newsapi") ? 2 : a.includes("gdelt") ? 3 : 9;
      const pb = b === "event_graph" ? 0 : b.includes("guardian") ? 1 : b.includes("newsapi") ? 2 : b.includes("gdelt") ? 3 : 9;
      return pa - pb;
    });

    const stories = [];
    while (stories.length < 10) {
      let pushed = false;
      for (const key of bucketOrder) {
        const arr = sourceBuckets.get(key) || [];
        if (!arr.length) continue;
        stories.push(arr.shift());
        pushed = true;
        if (stories.length >= 10) break;
      }
      if (!pushed) break;
    }

    const storyMarkup = stories.length
      ? stories.map((rec) => {
        const kind = sourceKind(rec.source);
        const text = `<span class="ticker-kind ${escapeHtml(kind.cls)}">${escapeHtml(kind.label)}</span><span class="ticker-source">${escapeHtml(sourceLabel(rec.source))}</span><span class="ticker-story">${escapeHtml(rec.title)}</span>`;
        if (rec.url && /^https?:\/\//i.test(rec.url)) {
          return `<a class="ticker-story-link" href="${escapeHtml(rec.url)}" target="_blank" rel="noopener noreferrer" title="Open source article in new tab">${text}</a>`;
        }
        return `<span class="ticker-story-link is-static">${text}</span>`;
      }).join('<span class="ticker-sep">•</span>')
      : `<span class="ticker-story-link is-static"><span class="ticker-story">${escapeHtml(fallback)}</span></span>`;

    const marquee = `
      <div class="ticker-marquee" aria-label="Headlines">
        <div class="ticker-marquee-track">
          ${storyMarkup}
          <span class="ticker-sep">•</span>
          ${storyMarkup}
          <span class="ticker-sep">•</span>
          ${storyMarkup}
        </div>
      </div>`;

    el.innerHTML = `
      <div class="ticker-stats">${stats.join("")}</div>
      ${marquee}`;

    const marqueeEl = el.querySelector(".ticker-marquee");
    const trackEl = el.querySelector(".ticker-marquee-track");
    if (marqueeEl && trackEl) {
      const pause = () => { trackEl.style.animationPlayState = "paused"; };
      const resume = () => { trackEl.style.animationPlayState = "running"; };
      marqueeEl.addEventListener("mouseenter", pause);
      marqueeEl.addEventListener("mouseleave", resume);
      marqueeEl.addEventListener("focusin", pause);
      marqueeEl.addEventListener("focusout", resume);
      marqueeEl.querySelectorAll(".ticker-story-link[href]").forEach((a) => {
        a.addEventListener("click", pause);
      });
    }

    renderSourceHealthPanel(ctx);
    renderFreshnessStrip();
  } catch (e) {
    el.innerHTML = `<span style="color:var(--muted); font-size:.75rem;">Market context unavailable.</span>`;
    renderSourceHealthPanel(null);
    renderFreshnessStrip();
  }
}

function pickRelevantHeadline(headlines) {
  if (!Array.isArray(headlines) || !headlines.length) return null;
  const include = /(\bsupply\s*chain\b|\benergy\b|\boil\b|\bgas\b|\blng\b|\bbrent\b|\bwti\b|\bshipping\b|\bfreight\b|\btanker\b|\bais\b|\bport\b|\bchokepoint\b|\bhormuz\b|\bsuez\b|\bbab[-\s]?el[-\s]?mandeb\b|\bstrait\b|\bopec\b|\bsanction\w*\b|\bofac\b|\bembargo\b|\brefiner\w*\b|\binventory\b|\beia\b|\bfred\b|\biran\b|\battack\b|\bstrike\b|\bconflict\b)/i;
  const exclude = /(tv tonight|alan carr|telly|celebrity|entertainment|movie|cinema|gossip|reality\s+show)/i;

  const cleaned = headlines
    .map((h) => String(h || "").trim())
    .filter(Boolean)
    .filter((h) => !exclude.test(h));

  if (!cleaned.length) return null;
  const ranked = cleaned
    .map((h) => ({ h, score: (h.match(include) || []).length }))
    .sort((a, b) => b.score - a.score);

  return ranked[0].score > 0 ? ranked[0].h : null;
}

async function ensureEvidenceCaches() {
  // Evidence chips should not depend on map style readiness.
  if (!twinCache || !Array.isArray(twinCache.chokepoints) || !twinCache.chokepoints.length || !Array.isArray(twinCache.tankers)) {
    try {
      twinCache = await apiFetch("/digital-twin/state", { timeoutMs: 45000 });
    } catch (e) {
      // best-effort only; render function will handle empty cache gracefully
    }
  }
  if (!marketCtxCache) {
    try {
      marketCtxCache = await apiFetch("/signals/market-context", { timeoutMs: 45000 });
    } catch (e) {
      // best-effort only
    }
  }
}

function renderReasoningEvidence() {
  const el = $("reasoningEvidence");
  if (!el) return;

  const chips = [];
  const tankers = (twinCache?.tankers || []).filter((t) => t && t.lat != null && t.lon != null).length;
  chips.push({
    action: "focus-map",
    html: `<span class="ev-chip"><strong>AIS</strong>${tankers} vessels tracked</span>`,
  });

  const cps = (twinCache?.chokepoints || []);
  if (cps.length) {
    const top = [...cps].sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0))[0];
    chips.push({
      action: "focus-map",
      html: `<span class="ev-chip"><strong>Corridor risk</strong>${escapeHtml(top.name)} ${Number(top.risk_score || 0).toFixed(2)}</span>`,
    });
  }

  if (marketCtxCache?.brent_usd != null) {
    chips.push({
      action: "scroll-forecast",
      html: `<span class="ev-chip"><strong>Brent</strong>$${Number(marketCtxCache.brent_usd).toFixed(2)}/bbl</span>`,
    });
  }
  if (marketCtxCache?.wti_usd != null) {
    chips.push({
      action: "scroll-forecast",
      html: `<span class="ev-chip"><strong>WTI</strong>$${Number(marketCtxCache.wti_usd).toFixed(2)}/bbl</span>`,
    });
  }

  // Weather proxy from live twin conditions (higher congestion/risk typically worsens marine ops).
  const hotPorts = (twinCache?.ports || []).filter((p) => Number(p.congestion || 0) >= 60).length;
  chips.push({
    action: "focus-map",
    html: `<span class="ev-chip"><strong>Weather/ops proxy</strong>${hotPorts} ports at high congestion</span>`,
  });

  if (Array.isArray(marketCtxCache?.recent_headlines) && marketCtxCache.recent_headlines.length) {
    chips.push({
      action: "scroll-map",
      html: `<span class="ev-chip"><strong>News feed</strong>${marketCtxCache.recent_headlines.length} recent headlines</span>`,
    });
  }

  const econ = lastPipelineDetails?.economic?.[0]?.recommendation_payload?.economic_impact;
  if (econ && econ.import_bill_delta_usd_bn != null) {
    chips.push({
      action: "scroll-actions",
      html: `<span class="ev-chip"><strong>Import bill \\u0394</strong>${fmtUsdMillionsFromBn(econ.import_bill_delta_usd_bn, { signed: true })}</span>`,
    });
  }
  if (econ && econ.cpi_delta_pct != null) {
    chips.push({
      action: "scroll-actions",
      html: `<span class="ev-chip"><strong>CPI passthrough</strong>+${Number(econ.cpi_delta_pct).toFixed(3)}%</span>`,
    });
  }

  el.innerHTML = chips
    .map((c) => `<button type="button" class="ev-chip-btn" data-action="${c.action}">${c.html}</button>`)
    .join("");
}

function flashAndScrollTo(id) {
  const el = $(id);
  if (!el) return;
  el.classList.remove("flash-target");
  void el.offsetWidth;
  el.classList.add("flash-target");
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------------------------------------------------------------------
// Causal Chain panel (PRD v2 Upgrade 2)
// ---------------------------------------------------------------------------

function renderCausalChain(hypothesis) {
  const flowEl = $("causalChainSteps");
  const affEl = $("affectedEntities");
  const srcEl = $("causalChainSource");

  const chain = hypothesis?.causal_chain;
  const fallback = hypothesis?.reasoning_chain || [];

  const kindLabels = {
    trigger: "Event trigger",
    chokepoint: "Chokepoint",
    route: "Shipping route",
    grade: "Crude supply",
    refinery: "Refinery",
    country: "Economic impact",
    spr_site: "Strategic reserve",
    misc: "Analysis",
  };

  const mechanismLabels = {
    event_trigger: "Event trigger",
    news_evidence: "News evidence",
    weather_branch: "Weather branch",
    security_branch: "Security branch",
    maritime_behavior: "Maritime behavior",
    throughput_merge: "Throughput merge",
    price_transmission: "Price transmission",
    india_exposure: "India exposure",
    recommendation: "Recommended actions",
    response_plan: "Recommended actions",
    scenario_projection: "Scenario projection",
    macro_impact: "Macro impact",
    price_signal: "Price signal",
    logistics_exposure: "Logistics exposure",
    ais_corridor_snapshot: "AIS corridor snapshot",
    weather_ops_proxy: "Weather/ops proxy",
    market_expectation: "Market expectation",
    maritime_channel_check: "Maritime channel check",
  };

  const evidenceTypeLabel = {
    observed: "Observed",
    derived: "Inferred",
    predicted: "Predicted",
  };

  const dotClass = (step) => {
    if (step.step_no === 1) return "trigger";
    const k = step.entity_kind;
    if (k === "chokepoint") return "chokepoint";
    if (k === "route") return "route";
    if (k === "grade") return "grade";
    if (k === "refinery") return "refinery";
    if (k === "country") return "country";
    if (k === "spr_site") return "spr_site";
    return "misc";
  };

  if (!chain && !fallback.length) {
    flowEl.innerHTML = `<p style="color:var(--muted); font-style:italic;">No reasoning chain available — trigger the pipeline to generate one.</p>`;
    if (affEl) affEl.innerHTML = "";
    if (srcEl) srcEl.textContent = "";
    return;
  }

  renderReasoningEvidence();

  if (chain && Array.isArray(chain.steps) && chain.steps.length) {
    if (srcEl) srcEl.textContent = `(${chain.source || "hybrid"} · ${chain.twin_branch_id || "live"} twin)`;
    flowEl.innerHTML = chain.steps.map((s) => {
      const cls = dotClass(s);
      const num = s.step_no;
      const mechKind = s.mechanism ? (mechanismLabels[s.mechanism] || s.mechanism.replaceAll("_", " ")) : "";
      const entityKind = s.entity_kind ? (kindLabels[s.entity_kind] || s.entity_kind) : "";
      const kind = mechKind || entityKind || (num === 1 ? "Event trigger" : "Analysis");
      const evType = (s.evidence_type || "").toLowerCase();
      const evLabel = evidenceTypeLabel[evType] || "";
      const evBadge = evLabel ? `<span class="ev-kind ev-${escapeHtml(evType)}">${escapeHtml(evLabel)}</span>` : "";
      const sources = Array.isArray(s.source_labels) ? s.source_labels.filter(Boolean).slice(0, 4) : [];
      const sourcesHtml = sources.length
        ? `<div class="cf-sources">Source: ${sources.map((x) => escapeHtml(x)).join(" · ")}</div>`
        : "";
      return `
        <div class="cf-step">
          <div class="cf-spine">
            <div class="cf-dot ${cls}">${num}</div>
          </div>
          <div class="cf-body">
            <div class="cf-kind">${escapeHtml(kind)}${s.entity_name ? " · " + escapeHtml(s.entity_name) : ""}${evBadge}</div>
            <div class="cf-claim">${escapeHtml(s.claim || "")}</div>
            ${sourcesHtml}
          </div>
        </div>`;
    }).join("");

    const aff = chain.affected || {};
    const groups = [
      ["Countries", aff.countries],
      ["Chokepoints", aff.chokepoints],
      ["Routes", aff.routes],
      ["Refineries", aff.refineries],
      ["Crude grades", aff.grades],
      ["SPR sites", aff.spr_sites],
    ]
      .filter(([, arr]) => Array.isArray(arr) && arr.length)
      .map(([label, arr]) => `<div class="aff-group"><strong>${label}</strong>${arr.slice(0, 5).join(", ")}${arr.length > 5 ? ` +${arr.length - 5}` : ""}</div>`);
    if (affEl) affEl.innerHTML = groups.join("");
    return;
  }

  // Fallback: render raw reasoning steps as flow cards
  if (srcEl) srcEl.textContent = "";
  flowEl.innerHTML = fallback.map((s, i) => `
    <div class="cf-step">
      <div class="cf-spine"><div class="cf-dot misc">${i + 1}</div></div>
      <div class="cf-body"><div class="cf-claim">${escapeHtml(s)}</div></div>
    </div>`).join("");
  if (affEl) affEl.innerHTML = "";
}



// ---------------------------------------------------------------------------
// Refinery Impact panel (PRD v2 Upgrade 5)
// ---------------------------------------------------------------------------

function renderRefinery(recs) {
  const listEl = $("refineryList");
  const exec = $("refExecSummary");
  const first = recs?.[0]?.recommendation_payload?.refinery_impact;
  if (!first) {
    listEl.innerHTML = "<p style='color:var(--muted);'>No refinery impact data loaded.</p>";
    $("refHealthy").textContent = "-";
    $("refWatch").textContent = "-";
    $("refCritical").textContent = "-";
    $("refAffectedCount").textContent = "-";
    $("refAffectedNames").textContent = "No major impacts yet.";
    if (exec) exec.textContent = "No refinery disruption signal detected yet.";
    return;
  }
  const all = first.refineries || [];
  const critical = all.filter((r) => r.starved || (Number(r.downtime_probability) || 0) >= 0.45 || Number(r.expected_utilization_pct || 0) < 60);
  const watch = all.filter((r) => !critical.includes(r) && ((Number(r.downtime_probability) || 0) >= 0.2 || Number(r.expected_utilization_pct || 0) < 80));
  const healthy = all.filter((r) => !critical.includes(r) && !watch.includes(r));
  const affected = [...critical, ...watch];

  $("refHealthy").textContent = String(healthy.length);
  $("refWatch").textContent = String(watch.length);
  $("refCritical").textContent = String(critical.length);
  $("refAffectedCount").textContent = String(affected.length);
  $("refAffectedNames").textContent = affected.length
    ? affected.slice(0, 6).map((r) => r.refinery_name).join(" · ")
    : "No major impacts yet.";
  if (exec) {
    if (!affected.length) exec.textContent = "Operational impact is low: all tracked refineries are healthy.";
    else exec.textContent = `Operational impact: ${critical.length} critical, ${watch.length} watchlist, ${healthy.length} healthy.`;
  }

  listEl.innerHTML = all
    .map((r) => {
      const util = Math.max(0, Math.min(100, r.expected_utilization_pct || 0));
      const rec = r.recommended_crude;
      const starved = r.starved
        ? `<span class="starved-tag">STARVED</span>`
        : "";
      const recTxt = rec
        ? `Recommended: <span class="r-crude">${escapeHtml(rec.grade_name || rec.grade_id)}</span> (${rec.source_country_iso3}${rec.reference_price_usd_bbl != null ? " @ $" + rec.reference_price_usd_bbl : ""})`
        : "Recommended: <em>no viable grade</em>";
      return `
        <div class="refinery-card ${r.starved ? "starved" : ""}">
          <div class="r-head">
            <strong>${escapeHtml(r.refinery_name)}</strong>
            ${starved}
          </div>
          <div style="color:var(--muted); font-size:.78rem;">${escapeHtml(r.operator || "")} · ${r.capacity_kbd} kbd cap</div>
          <div>Utilization: <strong>${r.expected_utilization_pct}%</strong> <span style="color:var(--muted);">(baseline ${r.baseline_utilization_pct}%)</span></div>
          <div class="util-bar"><span style="width:${util}%"></span></div>
          <div>Feedstock gap: <strong>${r.feedstock_gap_days}d</strong> · downtime prob: ${(r.downtime_probability * 100).toFixed(1)}%</div>
          <div style="font-size:.82rem; color:var(--muted); margin-top:4px;">${recTxt}</div>
        </div>`;
    })
    .join("");
}

// ---------------------------------------------------------------------------
// SPR + Replenishment panel (PRD v2 Upgrade 3)
// ---------------------------------------------------------------------------

function renderReplenishment(recs) {
  const preferredRec = pickPreferredPolicyRecommendation(recs, lastPipelineDetails?.simulations);
  const first = preferredRec?.recommendation_payload;
  const policy = first?.policy;
  const health = first?.reserve_health;
  const rl = $("reserveHealthList");
  const exec = $("sprExecSummary");

  if (!policy) {
    ["sprDraw", "sprTotal", "sprCover", "refillWhen", "refillVol", "refillSupplier",
     "refillGrade", "refillPrice", "refillSpot", "refillCost", "refillSavings"].forEach((id) => {
      $(id).textContent = "-";
    });
    $("sprStatus").textContent = "-";
    $("sprCoverageDays").textContent = "-";
    $("sprRecommendation").textContent = "-";
    $("refillRationale").textContent = "Awaiting pipeline data.";
    if (exec) exec.textContent = "No reserve policy action available yet.";
    rl.innerHTML = "";
    return;
  }

  const drawMbd = Number(policy.recommended_spr_draw_mbd_day1 ?? 0);
  const drawMbbl = Number(policy.total_draw_million_barrels ?? 0);
  const refill = policy.replenishment || {};
  const refillVol = Number(refill.refill_volume_mbbl ?? 0);
  const noAction = drawMbd === 0 && refillVol === 0;

  // Status banner injected just before the first metrics div via the rationale element.
  // We repurpose `refillRationale` as the status + rationale container.
  if (noAction) {
    $("sprDraw").textContent = "0 mbd";
    $("sprTotal").textContent = "None";
  } else {
    $("sprDraw").textContent = `${drawMbd.toFixed(3)} mbd`;
    $("sprTotal").textContent = `${drawMbbl.toFixed(2)} mbbl`;
  }

  if (health && health.days_of_import_cover_before != null) {
    const b = Math.round(Number(health.days_of_import_cover_before || 0));
    const a = Math.round(Number(health.days_of_import_cover_after_drawdown || 0));
    const r2 = Math.round(Number(health.days_of_import_cover_after_refill || 0));
    const color = b >= 90 ? "#22c55e" : b >= 60 ? "#f59e0b" : "#f43f5e";
    $("sprCover").innerHTML = `<span style="color:${color}">${b}d</span> → ${a}d → ${r2}d`;
  } else {
    $("sprCover").textContent = drawMbd === 0 ? "Adequate" : "—";
  }

  const beforeCover = Number(health?.days_of_import_cover_before ?? 0);
  if (noAction) {
    $("sprStatus").textContent = "Healthy";
    $("sprCoverageDays").textContent = beforeCover ? `${Math.round(beforeCover)} days` : "Adequate";
    $("sprRecommendation").textContent = "No draw required";
    if (exec) exec.textContent = "Recommendation: do not release SPR. Current disruption is below reserve-action threshold.";
  } else {
    $("sprStatus").textContent = drawMbd > 0 ? "Draw advised" : "Watch";
    $("sprCoverageDays").textContent = beforeCover ? `${Math.round(beforeCover)} days` : "-";
    $("sprRecommendation").textContent = "Execute policy plan";
    if (exec) exec.textContent = `Recommendation: execute SPR policy plan with ${drawMbd.toFixed(3)} mbd draw and controlled refill.`;
  }

  if (noAction) {
    $("refillWhen").textContent = "—";
    $("refillVol").textContent = "—";
    $("refillSupplier").textContent = "—";
    $("refillGrade").textContent = "—";
    $("refillPrice").textContent = "—";
    $("refillSpot").textContent = "—";
    $("refillCost").textContent = "—";
    $("refillSavings").textContent = "—";
    $("refillRationale").innerHTML =
      `<span style="color:#22c55e; font-weight:600;">✓ Reserves healthy</span> — no SPR drawdown or replenishment required for this scenario.`;
  } else {
    $("refillWhen").textContent = refill.when_day != null ? `Day ${refill.when_day}` : "—";
    $("refillVol").textContent = refillVol > 0 ? `${refillVol.toFixed(1)} mbbl` : "—";
    $("refillSupplier").textContent = refill.target_supplier_iso3 || "—";
    $("refillGrade").textContent = refill.target_grade_id ? refill.target_grade_id.replace("grade_", "").toUpperCase() : "—";
    $("refillPrice").textContent = refill.trigger_price_usd_bbl != null ? `$${refill.trigger_price_usd_bbl}/bbl` : "—";
    $("refillSpot").textContent = refill.spot_price_usd_bbl != null ? `$${refill.spot_price_usd_bbl}/bbl` : "—";
    $("refillCost").textContent = refill.estimated_cost_usd_bn != null ? fmtUsdMillionsFromBn(refill.estimated_cost_usd_bn) : "—";
    $("refillSavings").textContent = refill.estimated_savings_vs_spot_usd_bn != null
      ? `${fmtUsdMillionsFromBn(refill.estimated_savings_vs_spot_usd_bn)} vs spot` : "—";
    const rationale = refill.rationale || "";
    $("refillRationale").innerHTML =
      `<span style="color:#f59e0b; font-weight:600;">⚠ Drawdown recommended</span>${rationale ? " — " + escapeHtml(rationale) : ""}`;
  }

  if (health && Array.isArray(health.sites) && health.sites.length) {
    rl.innerHTML = health.sites
      .map((s) => {
        const fillPct = s.capacity_mbbl > 0
          ? Math.round((s.fill_after_refill_mbbl / s.capacity_mbbl) * 100)
          : 0;
        const fillColor = fillPct >= 70 ? "#22c55e" : fillPct >= 40 ? "#f59e0b" : "#f43f5e";
        return `
          <div style="border-top:1px solid #1e2f38; padding:6px 0;">
            <div style="font-weight:600; font-size:.85rem;">${escapeHtml(s.spr_site_name)}</div>
            <div style="font-size:.78rem; color:var(--muted); margin-top:2px;">
              Capacity: ${s.capacity_mbbl} mbbl &nbsp;·&nbsp;
              Current: ${s.fill_before_mbbl} → after draw: ${s.fill_after_drawdown_mbbl} → after refill: <strong style="color:${fillColor};">${s.fill_after_refill_mbbl} mbbl (${fillPct}%)</strong>
            </div>
          </div>`;
      })
      .join("");
  } else {
    rl.innerHTML = "<p style='color:var(--muted); font-size:.82rem;'>No per-site reserve data available.</p>";
  }
}

function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function updateExecutiveSummary(details) {
  const state = details?.state || {};
  const recConf = Number(state.reconciled_confidence ?? 0);
  const severity = recConf >= 0.7 ? "High" : recConf >= 0.45 ? "Moderate" : "Low";

  const chainSteps = details?.hypothesis?.causal_chain?.steps || [];
  const primary = chainSteps.find((s) => s.mechanism === "event_trigger")
    || chainSteps.find((s) => s.evidence_type === "observed")
    || null;
  const riskClaim = primary?.claim ? String(primary.claim).trim() : "";
  const maxRiskLen = 92;
  const shortRisk = riskClaim.length > maxRiskLen
    ? `${riskClaim.slice(0, maxRiskLen).replace(/\s+\S*$/, "").trim()}...`
    : riskClaim;
  $("exRisk").textContent = shortRisk || "Signal-driven event";
  $("exConf").textContent = `${severity} (${fmtPctWhole(recConf)})`;
  $("exSeverityNote").textContent = severity === "Low"
    ? "Low immediate operational impact under current conditions."
    : severity === "Moderate"
      ? "Moderate risk with watchlist-level operational impact."
      : "High risk with immediate operational and cost implications.";

  const driverSources = new Set();
  chainSteps.slice(0, 4).forEach((s) => (s.source_labels || []).forEach((lbl) => driverSources.add(lbl)));
  $("exDriver").textContent = driverSources.size
    ? Array.from(driverSources).slice(0, 2).join(" + ")
    : "Cross-source reasoning";

  const econ = details?.economic?.[0]?.recommendation_payload?.economic_impact;
  const opsLine = deriveOpsDisruptionLevel(details);
  if (econ?.import_bill_delta_usd_bn != null) {
    $("exIndia").innerHTML = `${escapeHtml(fmtUsdMillionsFromBn(econ.import_bill_delta_usd_bn, { signed: true }))} import cost<br><span class="muted-inline">${escapeHtml(opsLine)}</span>`;
  } else if (econ?.cpi_delta_pct != null) {
    $("exIndia").innerHTML = `CPI +${fmtPctWhole(Number(econ.cpi_delta_pct) / 100)}<br><span class="muted-inline">${escapeHtml(opsLine)}</span>`;
  } else {
    $("exIndia").innerHTML = `Impact currently limited<br><span class="muted-inline">${escapeHtml(opsLine)}</span>`;
  }

  const selectedCount = details?.procurement?.[0]?.recommendation_payload?.ranking?.filter((r) => r.status === "selected").length || 0;
  $("exAction").textContent = selectedCount ? `Maintain procurement (${selectedCount} selected)` : "Maintain current procurement";

  const preferredPolicyRec = pickPreferredPolicyRecommendation(details?.policy, details?.simulations);
  const draw = Number(preferredPolicyRec?.recommendation_payload?.policy?.recommended_spr_draw_mbd_day1 ?? 0);
  $("exSpr").textContent = draw > 0 ? "Draw recommended" : "No SPR action";

  const reasons = [];
  const chain = details?.hypothesis?.causal_chain;
  const observedCount = (chain?.steps || []).filter((s) => s.evidence_type === "observed").length;
  if (observedCount > 0) reasons.push(`Observed evidence across ${observedCount} chain step${observedCount === 1 ? "" : "s"}.`);

  const sourceSet = new Set();
  (chain?.steps || []).forEach((s) => (s.source_labels || []).forEach((lbl) => sourceSet.add(lbl)));
  if (sourceSet.size > 0) reasons.push(`Corroborated by ${sourceSet.size} source type${sourceSet.size === 1 ? "" : "s"}.`);

  let quality = "Low";
  if (observedCount >= 2 && sourceSet.size >= 3) quality = "High";
  else if (observedCount >= 1 && sourceSet.size >= 2) quality = "Medium";
  const stars = quality === "High" ? "★★★★★" : quality === "Medium" ? "★★★☆☆" : "★★☆☆☆";
  const qEl = $("exEvidenceQuality");
  qEl.textContent = `${stars} ${quality}`;
  qEl.classList.remove("eq-high", "eq-medium", "eq-low");
  qEl.classList.add(quality === "High" ? "eq-high" : quality === "Medium" ? "eq-medium" : "eq-low");
  $("exQualityDetail").textContent = `${sourceSet.size} independent sources · ${observedCount} observed signal steps · ${(chain?.steps || []).filter((s) => s.evidence_type === "predicted").length} predicted steps`;

  if (state.disagreement) reasons.push("Red-team disagreement detected and confidence reconciled downward.");
  else reasons.push("No material red-team contradiction after reconciliation.");

  const procTop = details?.procurement?.[0]?.recommendation_payload?.ranking?.find((r) => r.status === "selected");
  const pass = (procTop?.constraints || []).filter((c) => c.satisfied).length;
  if (pass > 0) reasons.push(`Top procurement option passes ${pass} key feasibility checks.`);

  const savings = Number(preferredPolicyRec?.recommendation_payload?.policy?.replenishment?.estimated_savings_vs_spot_usd_bn ?? NaN);
  if (Number.isFinite(savings) && savings > 0) {
    $("exOutcome").textContent = `${fmtUsdMillionsFromBn(savings, { approx: true })} savings potential`;
  } else {
    $("exOutcome").textContent = "Continuity and cost stability maintained";
  }

  $("exConfWhy").innerHTML = reasons.slice(0, 4).map((r) => `<li>${escapeHtml(r)}</li>`).join("") || "<li>Awaiting evidence.</li>";
}

function updateConfidenceDecomposition(details) {
  const state = details?.state || {};
  const chain = details?.hypothesis?.causal_chain;
  const steps = chain?.steps || [];
  const sims = details?.simulations || [];

  const observed = steps.filter((s) => String(s.evidence_type || "").toLowerCase() === "observed").length;
  const predicted = steps.filter((s) => String(s.evidence_type || "").toLowerCase() === "predicted").length;
  const sourceSet = new Set();
  steps.forEach((s) => (s.source_labels || []).forEach((lbl) => sourceSet.add(lbl)));

  const evidenceScore = Math.max(0.2, Math.min(0.95, 0.30 + (observed * 0.10) + (sourceSet.size * 0.07) - (predicted * 0.03)));
  const reasoningBase = Number(state.hypothesis_confidence ?? 0);
  const reasoningScore = Number.isFinite(reasoningBase) && reasoningBase > 0 ? reasoningBase : Math.max(0.25, Math.min(0.9, evidenceScore - 0.08));

  const simMain = sims.find((s) => s.horizon === "1wk") || sims[0] || null;
  const simProb = Number(simMain?.percentiles?.disruption_prob ?? NaN);
  const simulationScore = Number.isFinite(simProb)
    ? Math.max(0.2, Math.min(0.95, 1 - Math.abs(simProb - 0.5)))
    : Math.max(0.25, Math.min(0.85, reasoningScore - 0.04));

  const finalScore = Number(state.reconciled_confidence ?? reasoningScore);
  const disagreement = !!state.disagreement;

  $("cdEvidence").textContent = fmtPctWhole(evidenceScore);
  $("cdReasoning").textContent = fmtPctWhole(reasoningScore);
  $("cdSimulation").textContent = fmtPctWhole(simulationScore);
  $("cdFinal").textContent = fmtPctWhole(finalScore);

  $("cdEvidenceWhy").textContent = `${sourceSet.size} source types · ${observed} observed steps`;
  $("cdReasoningWhy").textContent = disagreement ? "Counter-reasoning reduced consistency" : "Causal chain checks are coherent";
  $("cdSimulationWhy").textContent = simMain ? `${simMain.horizon} horizon · distribution stability checked` : "Simulation payload pending";
  $("cdFinalWhy").textContent = disagreement
    ? "Reconciled after counter-evidence"
    : "Final reflects consistent evidence";
  if (finalScore + 0.2 < evidenceScore) {
    $("cdFinalWhy").textContent = "Decision confidence is intentionally conservative vs evidence due uncertainty controls";
  }
}

function updateTrustPathCompletion(details) {
  const hasEvidence = (details?.hypothesis?.causal_chain?.steps || []).length > 0;
  const hasReasoning = !!details?.hypothesis?.hypothesis_text;
  const hasCounter = !!details?.redteam?.rebuttal_text || !!$("rebuttalText")?.textContent;
  const hasDecision = (details?.procurement || []).length > 0;
  const hasValidation = true;

  const stateMap = {
    evidence: hasEvidence,
    reasoning: hasReasoning,
    counter: hasCounter,
    decision: hasDecision,
    validation: hasValidation,
  };
  document.querySelectorAll("#trustPathCard .trust-step[data-step]").forEach((btn) => {
    const done = !!stateMap[btn.dataset.step];
    btn.classList.toggle("done", done);
  });
}

function updateCareBridge(details) {
  const econ = details?.economic?.[0]?.recommendation_payload?.economic_impact;
  const primary = details?.hypothesis?.causal_chain?.steps?.find((s) => s.mechanism === "event_trigger")
    || details?.hypothesis?.causal_chain?.steps?.find((s) => s.evidence_type === "observed");
  const signal = primary?.claim || "Current geopolitical signal";
  const impact = econ?.import_bill_delta_usd_bn != null
    ? `raises India's import cost by about ${fmtUsdMillionsFromBn(econ.import_bill_delta_usd_bn)}`
    : "creates benchmark-price pressure for India";
  const ops = details?.hypothesis?.causal_chain?.steps?.some((s) => s.mechanism === "maritime_channel_check")
    ? "while shipping constraints remain limited"
    : "with direct logistics pressure";
  $("careBridge").textContent = `${signal}. This typically transmits through benchmark prices, ${impact}, ${ops}.`;
}

function updateWhyNow(details) {
  const chainSteps = details?.hypothesis?.causal_chain?.steps || [];
  const observed = chainSteps.find((s) => String(s.evidence_type || "").toLowerCase() === "observed") || chainSteps[0];
  const trigger = observed?.claim || details?.hypothesis?.hypothesis_text || "A market-risk signal is active";

  const sim = (details?.simulations || []).find((s) => s?.horizon === "1wk") || (details?.simulations || [])[0] || null;
  const p = sim?.percentiles || {};
  const p10 = Number(p.p10_price_shock_pct ?? NaN);
  const p90 = Number(p.p90_price_shock_pct ?? NaN);
  const lo = Number.isFinite(p10) ? Math.max(1, Math.round(p10 * 100)) : 2;
  const hi = Number.isFinite(p90) ? Math.max(lo + 1, Math.round(p90 * 100)) : 5;

  const text = `${trigger}. Historically, similar conditions have transmitted into roughly ${lo}-${hi}% benchmark-price pressure over the next week, which can move import costs before physical logistics are disrupted.`;
  const el = $("whyNowText");
  if (el) el.textContent = text;
}

function updateMissionBar(details) {
  const state = details?.state || {};
  const ranking = details?.procurement?.[0]?.recommendation_payload?.ranking || [];
  const selected = ranking.filter((r) => r.status === "selected");
  const throughputKbd = selected.reduce((sum, r) => sum + Number(r.allocated_kbd || 0), 0);

  const preferredPolicyRec = pickPreferredPolicyRecommendation(details?.policy, details?.simulations);
  const health = preferredPolicyRec?.recommendation_payload?.reserve_health;
  const cover = Number(health?.days_of_import_cover_before ?? NaN);

  const conf = Number(state.reconciled_confidence ?? state.hypothesis_confidence ?? 0);
  const risk = conf < 0.45 || state.disagreement ? "High" : conf < 0.7 ? "Moderate" : "Low";
  const status = risk === "High" ? "Watch" : risk === "Moderate" ? "Guarded" : "Healthy";

  const autoObjective = throughputKbd > 0
    ? `Maintain ${Math.round(throughputKbd).toLocaleString()} kbd import coverage`
    : "Maintain coverage of 1800 kbd";

  const objectiveEl = $("missionObjective");
  if (objectiveEl) {
    const customObjective = (() => { try { return localStorage.getItem("kavach.missionObjective") || ""; } catch { return ""; } })();
    objectiveEl.textContent = customObjective || autoObjective;
    objectiveEl.dataset.autoText = autoObjective;
  }

  const throughputEl = $("missionThroughput");
  if (throughputEl) throughputEl.textContent = throughputKbd > 0 ? `${Math.round(throughputKbd).toLocaleString()} kbd` : "Pending";

  const statusEl = $("missionStatus");
  if (statusEl) statusEl.textContent = status;

  const riskEl = $("missionRisk");
  if (riskEl) riskEl.textContent = risk;

  const coverageEl = $("missionCoverage");
  if (coverageEl) coverageEl.textContent = Number.isFinite(cover) && cover > 0 ? `${Math.round(cover)} days` : "n/a";
}

function inferMissionObjective() {
  const text = String($("missionObjective")?.textContent || "").toLowerCase();
  if (!text) return "balanced_resilience";
  if (/cost|import cost|minimi[sz]e|savings|cheapest|price/.test(text)) return "minimize_import_cost";
  if (/resilien|security|continuity|surviv|redundan|uptime|protect/.test(text)) return "maximize_supply_resilience";
  if (/coverage|throughput|maintain|availability|supply/.test(text)) return "maintain_import_coverage";
  return "balanced_resilience";
}

function getImportBudgetUsdBn() {
  const el = $("missionImportBudget");
  if (!el) return null;
  const raw = String(el.value || "").trim();
  if (!raw) return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return null;
  return Math.round(n * 100) / 100;
}

function updateBudgetHint(details) {
  const hint = $("missionBudgetActive");
  if (!hint) return;
  const eff = Number(details?.effective_import_budget_usd_bn);
  if (!Number.isFinite(eff) || eff <= 0) {
    hint.textContent = "";
    hint.classList.remove("is-active");
    hint.removeAttribute("title");
    return;
  }
  const userVal = getImportBudgetUsdBn();
  const source = userVal !== null && Math.abs(userVal - eff) < 0.01 ? "your input" : "default";
  hint.textContent = `active: $${eff} bn/yr (${source})`;
  hint.title = `Economic model ran with annual import budget = $${eff} bn/yr`;
  hint.classList.add("is-active");
}

function bindImportBudgetInput() {
  const el = $("missionImportBudget");
  if (!el || el.dataset.budgetBound === "true") return;
  el.dataset.budgetBound = "true";
  try {
    const saved = localStorage.getItem("kavach.importBudgetUsdBn");
    if (saved && Number(saved) > 0) el.value = saved;
  } catch { /* ignore */ }
  const commit = () => {
    const n = getImportBudgetUsdBn();
    try {
      if (n === null) localStorage.removeItem("kavach.importBudgetUsdBn");
      else localStorage.setItem("kavach.importBudgetUsdBn", String(n));
    } catch { /* ignore */ }
    setStatus(n ? `budget set to $${n} bn/yr` : "budget reset to default", "ok");
  };
  el.addEventListener("change", commit);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); el.blur(); }
  });
}

function bindMissionObjectiveEditor() {
  const el = $("missionObjective");
  if (!el || el.dataset.editorBound === "true") return;
  el.dataset.editorBound = "true";
  el.setAttribute("contenteditable", "true");
  el.setAttribute("spellcheck", "false");
  el.setAttribute("role", "textbox");
  el.setAttribute("aria-label", "Mission objective — click to edit");
  el.title = "Click to set your own mission. Press Enter to save, Esc to cancel.";

  const commit = () => {
    const value = (el.textContent || "").trim();
    const autoText = el.dataset.autoText || "";
    try {
      if (!value || value === autoText) {
        localStorage.removeItem("kavach.missionObjective");
      } else {
        localStorage.setItem("kavach.missionObjective", value);
      }
    } catch { /* localStorage unavailable is fine */ }
    setStatus(value ? "mission updated" : "mission reset to auto", "ok");
  };

  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      el.blur();
    } else if (e.key === "Escape") {
      e.preventDefault();
      try {
        const saved = localStorage.getItem("kavach.missionObjective");
        el.textContent = saved || el.dataset.autoText || el.textContent;
      } catch { /* ignore */ }
      el.blur();
    }
  });
  el.addEventListener("blur", commit);

  el.addEventListener("dblclick", () => {
    try { localStorage.removeItem("kavach.missionObjective"); } catch { /* ignore */ }
    el.textContent = el.dataset.autoText || "Maintain coverage of 1800 kbd";
    setStatus("mission reset to auto (double-click)", "ok");
  });
}

async function refreshPipelineDetails() {
  const pipelineId = $("pipelineId").value.trim();
  if (!pipelineId) {
    setStatus("pipeline id missing", "err");
    return;
  }
  setStatus("Updating Intelligence...");
  const details = await apiFetch(`/pipeline/${pipelineId}/details`, { timeoutMs: 120000 });
  clearPropagationOverlay();

  const state = details.state;
  $("hypConf").textContent = fmtPctWhole(state.hypothesis_confidence);
  $("recConf").textContent = fmtPctWhole(state.reconciled_confidence);
  $("disagreeFlag").textContent = state.disagreement ? "YES" : "NO";

  $("hypText").textContent = details.hypothesis?.hypothesis_text || "-";
  renderCausalChain(details.hypothesis);

  drawTimeline(details.simulations || [], mcState.branchScenario, details.economic || []);
  renderProcurement(details.procurement || []);
  renderRefinery(details.refinery || []);
  renderReplenishment(details.policy || []);

  // Keep red-team rendering isolated so it can never block action panels.
  try {
    const assessment = buildDynamicRedTeamAssessment(details);
    $("rebuttalText").textContent = assessment.text || "-";
    const rtMeta = $("rebuttalMeta");
    if (assessment.counterConfidence != null) {
      rtMeta.style.display = "";
      const pct = Math.round(Number(assessment.counterConfidence) * 100);
      const barColor = pct >= 65 ? "#f43f5e" : pct >= 45 ? "#f59e0b" : "#22c55e";
      const level = pct >= 65 ? "Strong" : pct >= 45 ? "Moderate" : "Light";
      $("rtStrengthBar").style.width = `${pct}%`;
      $("rtStrengthBar").style.background = barColor;
      $("rtStrengthPct").textContent = `${pct}%`;
      $("rtStrengthPct").style.color = barColor;
      const lvlEl = $("rtStrengthLevel");
      if (lvlEl) lvlEl.textContent = level;
      const impactEl = $("rtImpact");
      if (impactEl) {
        impactEl.textContent = assessment.impact;
      }
      const signals = assessment.disproofSignals || [];
      $("disproofList").innerHTML = signals.length
        ? signals.map((s) => `<li>${escapeHtml(s)}</li>`).join("")
        : "";
    } else if (rtMeta) {
      rtMeta.style.display = "none";
    }
  } catch (err) {
    console.warn("Red-team render fallback", err);
    const rtMeta = $("rebuttalMeta");
    if (rtMeta) rtMeta.style.display = "none";
    $("rebuttalText").textContent = details.redteam?.rebuttal_text || "-";
  }

  updateExecutiveSummary(details);
  updateConfidenceDecomposition(details);
  updateTrustPathCompletion(details);
  updateCareBridge(details);
  updateWhyNow(details);
  renderMapStoryFlow(details);
  updateMissionBar(details);
  updateWorldStateFooter(details);
  await ensureEvidenceCaches();
  renderReasoningEvidence();
  updateMapRisk(state, details);
  updateIncidentFeed(details);
  fitMapToCausalChain(details.hypothesis?.causal_chain);

  setStatus("Pipeline Ready", "ok");
}

// Zoom the map onto the entities the current hypothesis's causal chain
// implicates. Falls back silently if nothing resolvable is affected.
function fitMapToCausalChain(chain) {
  if (!warMap || !twinCache || !chain?.affected) return;
  const points = [];
  const portById = Object.fromEntries((twinCache.ports || []).map((p) => [p.id, p]));
  for (const cpId of chain.affected.chokepoints || []) {
    const cp = (twinCache.chokepoints || []).find((c) => c.id === cpId);
    if (cp) points.push([cp.lon, cp.lat]);
  }
  for (const rId of chain.affected.routes || []) {
    const route = (twinCache.routes || []).find((r) => r.id === rId);
    if (!route) continue;
    const o = portById[route.origin_port_id];
    const d = portById[route.destination_port_id];
    if (o) points.push([o.lon, o.lat]);
    if (d) points.push([d.lon, d.lat]);
  }
  for (const refId of chain.affected.refineries || []) {
    const ref = (twinCache.refineries || []).find((r) => r.id === refId);
    if (ref && ref.lat != null && ref.lon != null) points.push([ref.lon, ref.lat]);
  }
  if (points.length >= 2) fitBoundsToPoints(points, 90);
}

async function initLatestEvent() {
  try {
    const signals = await apiFetch("/signals/recent-live?limit=1");
    const label = $("eventLabel");
    if (signals && signals.length > 0) {
      const latest = signals[0];
      latestLiveSignalTs = latest.event_ts || null;
      renderFreshnessStrip();
      $("eventId").value = latest.structured_event_id || latest.id || 1;
      if (label) label.textContent = `Latest: ${escapeHtml(latest.target || String(latest.structured_event_id || latest.id))} · ${escapeHtml(latest.action_type || "")}`;
    } else {
      if (label) label.textContent = "No live news event loaded yet";
    }
  } catch (e) {
    console.warn("initLatestEvent failed", e);
  }
}

async function triggerPipeline() {
  const eventId = Number($("eventId").value);
  if (!eventId || eventId < 1) {
    setStatus("invalid event id", "err");
    return;
  }
  const btn = $("triggerBtn");
  if (btn) btn.dataset.state = "running";
  setPipelineProgress("reasoning");
  setStatus("Updating Intelligence...");
  try {
    const missionObjective = inferMissionObjective();
    const importBudget = getImportBudgetUsdBn();
    const body = { structured_event_id: eventId, mission_objective: missionObjective };
    if (importBudget !== null) body.annual_import_budget_usd_bn = importBudget;
    const out = await apiFetch("/pipeline/trigger", {
      method: "POST",
      body: JSON.stringify(body),
    });
    setPipelineProgress("reasoning");
    $("pipelineId").value = out.pipeline_id;
    setStatus("Pipeline Ready", "ok");
    await refreshPipelineDetails();
    setPipelineProgress("recommendation");
    completePipelineProgress();
    if (btn) btn.dataset.state = "on";
  } catch (e) {
    if (btn) btn.dataset.state = "err";
    throw e;
  }
}

async function showIncidents(nodeOverride = null) {
  const node = (nodeOverride || $("entitySelect").value || "").trim();
  if (!node) {
    setStatus("pick an entity first", "err");
    return;
  }
  const listEl = $("incidentList");
  listEl.innerHTML = "<p class='incident-empty'>Loading…</p>";
  const out = await apiFetch(`/kg/history?node=${encodeURIComponent(node)}&limit=15`);
  renderIncidents(out, node);
  focusMapOnEntity(node);
}

function pickIncidentNodeFromContext() {
  const chain = lastPipelineDetails?.hypothesis?.causal_chain;
  if (!chain || !twinCache) return null;

  const cpId = chain.affected?.chokepoints?.[0];
  if (cpId) {
    const cp = (twinCache.chokepoints || []).find((c) => c.id === cpId);
    if (cp?.name) return cp.name;
  }

  const routeId = chain.affected?.routes?.[0];
  if (routeId) {
    const route = (twinCache.routes || []).find((r) => r.id === routeId);
    if (route) {
      const port = (twinCache.ports || []).find((p) => p.id === route.destination_port_id)
        || (twinCache.ports || []).find((p) => p.id === route.origin_port_id);
      if (port?.name) return port.name;
    }
  }

  const country = chain.affected?.countries?.[0];
  if (country) return country;

  return null;
}

// Zoom the map onto an entity and drop a pulsing highlight on it.
function focusMapOnEntity(node) {
  if (!warMap) return;
  const lower = node.toLowerCase();
  const port = (twinCache?.ports || []).find((p) => p.name.toLowerCase().includes(lower) || p.id === lower);
  const cp = (twinCache?.chokepoints || []).find((c) => c.name.toLowerCase().includes(lower) || c.id === lower);
  const ref = (twinCache?.refineries || []).find((r) => r.name.toLowerCase().includes(lower) || r.id === lower);
  const country = (twinCache?.countries || []).find((c) => c.name.toLowerCase() === lower);

  let dest = null;
  let kind = null;
  let neighbours = [];
  if (port) { dest = [port.lon, port.lat]; kind = "port"; }
  else if (cp) { dest = [cp.lon, cp.lat]; kind = "chokepoint";
    // Show related routes as neighbours to auto-fit
    (twinCache?.routes || []).forEach((r) => {
      if ((r.chokepoint_ids || []).includes(cp.id)) {
        const o = twinCache.ports.find((p) => p.id === r.origin_port_id);
        const d = twinCache.ports.find((p) => p.id === r.destination_port_id);
        if (o) neighbours.push([o.lon, o.lat]);
        if (d) neighbours.push([d.lon, d.lat]);
      }
    });
  } else if (ref && ref.lat != null && ref.lon != null) { dest = [ref.lon, ref.lat]; kind = "refinery"; }
  else if (country) {
    // Fit to all twin ports for that country
    const ps = twinCache.ports.filter((p) => p.country_iso3 === country.iso3);
    if (ps.length) {
      fitBoundsToPoints(ps.map((p) => [p.lon, p.lat]), 80);
      updateFocusMarker(null);
      return;
    }
  }

  if (!dest) return;
  updateFocusMarker(dest);
  if (neighbours.length) {
    fitBoundsToPoints([dest, ...neighbours], 80);
  } else {
    warMap.flyTo({ center: dest, zoom: Math.max(warMap.getZoom(), kind === "chokepoint" ? 4.8 : 5.5), essential: true, duration: 900 });
  }
}

function updateFocusMarker(coords) {
  if (!warMap || !warMap.getSource("focus-src")) return;
  const data = coords
    ? { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: coords }, properties: {} }] }
    : { type: "FeatureCollection", features: [] };
  warMap.getSource("focus-src").setData(data);
}

function clearPropagationOverlay() {
  const empty = { type: "FeatureCollection", features: [] };
  if (warMap?.getSource("prop-point-src")) warMap.getSource("prop-point-src").setData(empty);
  if (warMap?.getSource("prop-route-src")) warMap.getSource("prop-route-src").setData(empty);
}

function resolveStepGeometry(step) {
  if (!step || !twinCache) return null;

  if (step.entity_kind === "chokepoint") {
    const cp = (twinCache.chokepoints || []).find((c) => c.id === step.entity_id)
      || (twinCache.chokepoints || []).find((c) => String(c.name || "").toLowerCase() === String(step.entity_name || "").toLowerCase());
    if (cp) return { point: [cp.lon, cp.lat] };
  }

  if (step.entity_kind === "refinery") {
    const ref = (twinCache.refineries || []).find((r) => r.id === step.entity_id)
      || (twinCache.refineries || []).find((r) => String(r.name || "").toLowerCase() === String(step.entity_name || "").toLowerCase());
    if (ref && ref.lat != null && ref.lon != null) return { point: [ref.lon, ref.lat] };
  }

  if (step.entity_kind === "route") {
    const route = findRouteByStep(step);
    if (route) {
      const o = (twinCache.ports || []).find((p) => p.id === route.origin_port_id);
      const d = (twinCache.ports || []).find((p) => p.id === route.destination_port_id);
      if (o && d) {
        return {
          point: [(o.lon + d.lon) / 2, (o.lat + d.lat) / 2],
          route: [[o.lon, o.lat], [d.lon, d.lat]],
        };
      }
    }
  }

  const cp = (twinCache.chokepoints || []).slice().sort((a, b) => Number(b.risk_score || 0) - Number(a.risk_score || 0))[0];
  if (cp) return { point: [cp.lon, cp.lat] };
  return null;
}

function paintPropagationStep(stepGeom) {
  if (!warMap) return;
  const pointData = stepGeom?.point
    ? {
        type: "FeatureCollection",
        features: [{ type: "Feature", geometry: { type: "Point", coordinates: stepGeom.point }, properties: {} }],
      }
    : { type: "FeatureCollection", features: [] };
  const routeData = stepGeom?.route
    ? {
        type: "FeatureCollection",
        features: [{ type: "Feature", geometry: { type: "LineString", coordinates: stepGeom.route }, properties: {} }],
      }
    : { type: "FeatureCollection", features: [] };

  if (warMap.getSource("prop-point-src")) warMap.getSource("prop-point-src").setData(pointData);
  if (warMap.getSource("prop-route-src")) warMap.getSource("prop-route-src").setData(routeData);
}

async function playCausalPropagation(chain, opts = {}) {
  if (!chain?.steps?.length || !warMap || !twinCache) return;
  const runId = ++propagationRunId;
  const caption = $("mapStoryCaption");
  const perStepMs = opts.perStepMs ?? 900;

  for (let i = 0; i < chain.steps.length; i += 1) {
    const step = chain.steps[i];
    if (runId !== propagationRunId) return;
    setActiveStoryFlow(Math.min(i, 3));
    const geom = resolveStepGeometry(step);
    if (!geom) continue;
    paintPropagationStep(geom);
    if (geom.point) {
      warMap.flyTo({ center: geom.point, zoom: Math.max(4.4, Math.min(6.1, warMap.getZoom())), essential: true, duration: 520 });
    }
    if (caption && opts.updateCaption) {
      const kind = step.mechanism ? String(step.mechanism).replaceAll("_", " ") : String(step.entity_kind || "analysis");
      caption.textContent = `${kind}: ${String(step.claim || "").slice(0, 96)}`;
    }
    await new Promise((resolve) => setTimeout(resolve, perStepMs));
  }
}

function fitBoundsToPoints(points, padding = 60) {
  if (!warMap || !points.length) return;
  const lons = points.map((p) => p[0]);
  const lats = points.map((p) => p[1]);
  const bounds = [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ];
  warMap.fitBounds(bounds, { padding, duration: 900, maxZoom: 6 });
}

function renderIncidents(response, node) {
  const listEl = $("incidentList");
  const items = response?.history || [];
  if (!items.length) {
    const summary = deriveEntityExposure(node);
    listEl.innerHTML = `
      <div class='incident-empty'>
        <p><strong>${escapeHtml(node)}</strong></p>
        <p>No active incidents in the current window.</p>
        <p>Current exposure: <strong>${escapeHtml(summary.level)}</strong></p>
        <p>Reason: ${escapeHtml(summary.reason)}</p>
      </div>`;
    $("entityHint").textContent = `${node}: no active incidents; exposure is ${summary.level.toLowerCase()}.`;
    return;
  }
  $("entityHint").textContent = `${node}: ${items.length} incident${items.length === 1 ? "" : "s"} in the current window.`;
  listEl.innerHTML = items.map(formatIncidentCard).join("");
}

function deriveEntityExposure(node) {
  const details = lastPipelineDetails;
  const chain = details?.hypothesis?.causal_chain;
  if (!chain) return { level: "LOW", reason: "Insufficient chain evidence for this window." };

  const lower = String(node || "").toLowerCase();
  const touchedByRoute = (chain.affected?.routes || []).some((rid) => String(rid).toLowerCase().includes(lower));
  const touchedByCountry = (chain.affected?.countries || []).some((c) => String(c).toLowerCase().includes(lower));
  const touchedByChoke = (twinCache?.chokepoints || []).some((cp) => {
    const nameHit = String(cp.name || "").toLowerCase().includes(lower);
    if (!nameHit) return false;
    return (chain.affected?.chokepoints || []).includes(cp.id);
  });
  const touched = touchedByRoute || touchedByCountry || touchedByChoke;
  const maxRisk = Math.max(0, ...(twinCache?.chokepoints || []).map((cp) => Number(cp.risk_score || 0)));
  const econ = details?.economic?.[0]?.recommendation_payload?.economic_impact;
  const costShockBn = Number(econ?.import_bill_delta_usd_bn ?? 0);

  if (touched && maxRisk >= 0.7) {
    return { level: "HIGH", reason: "Entity sits on an active risk path with elevated corridor risk." };
  }
  if (touched || costShockBn > 0) {
    return { level: "MODERATE", reason: "Primary impact appears price-linked; no direct logistics break detected." };
  }
  return { level: "LOW", reason: "No direct logistics disruption path detected for this entity." };
}

function formatIncidentCard(item) {
  const actionType = item.action_type || "signal";
  const target = item.target || "—";
  const actors = Array.isArray(item.actors) ? item.actors.slice(0, 4).join(", ") : "";
  const when = formatWhen(item.event_ts);
  return `
    <div class="incident-card">
      <div class="ic-head">
        <span class="ic-type">${escapeHtml(actionType)}</span>
        <span class="ic-when">${escapeHtml(when)}</span>
      </div>
      <div class="ic-target">Target: ${escapeHtml(target)}</div>
      ${actors ? `<div class="ic-actors">Actors: ${escapeHtml(actors)}</div>` : ""}
    </div>`;
}

function formatWhen(iso) {
  if (!iso) return "unknown time";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return String(iso);
  const diffSec = (Date.now() - t) / 1000;
  if (diffSec < 15) return "just now";
  if (diffSec < 86400 * 60) return formatRelativeAgeSeconds(diffSec, { short: false });
  return new Date(t).toISOString().slice(0, 10);
}

async function runWhatIf() {
  // Legacy demand-only what-if. Preserved for API back-compat only; the
  // Scenario Console in the map card is the recommended entry point.
  throw new Error("legacy demand-only what-if is retired — use the Scenario Console");
}

async function runBacktest() {
  const start = $("btStart").value.trim();
  const end = $("btEnd").value.trim();
  if (!start || !end) {
    setStatus("pick validation start + end", "err");
    return;
  }
  const startIso = new Date(start).toISOString();
  const endIso = new Date(end).toISOString();
  if (Date.parse(startIso) >= Date.parse(endIso)) {
    setStatus("start must be before end", "err");
    return;
  }
  const btDetails = $("btDetails");
  if (btDetails) btDetails.open = true;
  setStatus("running validation…");
  renderBacktestLoading();
  const out = await apiFetch("/backtest", {
    method: "POST",
    body: JSON.stringify({ start: startIso, end: endIso }),
  });
  renderBacktest(out);
  setStatus(`validation complete · ${out.events} events`, "ok");
}

// ---------------------------------------------------------------------------
// Backtest panel — historical validation of the pipeline
// ---------------------------------------------------------------------------

const btState = {
  runs: [],
  sortKey: "event_ts",
  sortDir: "desc",
  selectedId: null,
};

function renderBacktestLoading() {
  $("btEvents").textContent = "…";
  $("btCalib").textContent = "…";
  $("btCalibLabel").textContent = "measuring";
  $("btCalibLabel").className = "bt-gauge-label";
  $("btCalibBar").style.width = "0%";
  $("btCalibBar").className = "bt-gauge-fill";
  $("btDisagreeRate").textContent = "…";
  $("btDisagreeCount").textContent = "";
  $("btMeanConf").textContent = "…";
  const accEl = $("btAccuracy");
  if (accEl) accEl.textContent = "…";
  const trust = $("btTrustSummary");
  if (trust) trust.textContent = "Running historical replay to estimate trust and consistency...";
  $("btTableBody").innerHTML = `<tr><td colspan="11" class="bt-empty">Running…</td></tr>`;
  const ctx = $("btTimeline").getContext("2d");
  ctx.clearRect(0, 0, $("btTimeline").width, $("btTimeline").height);
}

function renderBacktest(out) {
  const events = out?.events ?? 0;
  $("btEvents").textContent = events;

  const calib = out?.calibration_score;
  if (calib != null) {
    const pct = Math.round(calib * 100);
    $("btCalib").textContent = calib.toFixed(3);
    $("btCalibBar").style.width = `${pct}%`;
    const tier =
      calib >= 0.85 ? "tier-strong"
      : calib >= 0.70 ? "tier-good"
      : "tier-weak";
    const label =
      calib >= 0.85 ? "Excellent (agents agree)"
      : calib >= 0.70 ? "Good (moderate spread)"
      : "Weak (frequent disagreement)";
    $("btCalibBar").className = `bt-gauge-fill ${tier}`;
    $("btCalibLabel").className = `bt-gauge-label ${tier}`;
    $("btCalibLabel").textContent = label;
  } else {
    $("btCalib").textContent = "-";
    $("btCalibBar").style.width = "0%";
    $("btCalibBar").className = "bt-gauge-fill";
    $("btCalibLabel").textContent = "no signal";
    $("btCalibLabel").className = "bt-gauge-label";
  }

  const rate = out?.disagreement_rate;
  if (rate != null) {
    $("btDisagreeRate").textContent = `${(rate * 100).toFixed(1)}%`;
    const flagged = Math.round(rate * events);
    $("btDisagreeCount").textContent = `${flagged} of ${events} events flagged`;
  } else {
    $("btDisagreeRate").textContent = "-";
    $("btDisagreeCount").textContent = "";
  }

  const mh = out?.mean_hypothesis_confidence;
  const mr = out?.mean_reconciled_confidence;
  if (mh != null && mr != null) {
    $("btMeanConf").textContent = `${mh.toFixed(2)} → ${mr.toFixed(2)}`;
  } else {
    $("btMeanConf").textContent = "-";
  }

  const accEl = $("btAccuracy");
  if (accEl) {
    const acc = out?.accuracy_rate;
    accEl.textContent = acc != null ? `${(acc * 100).toFixed(1)}%` : "-";
  }
  const trust = $("btTrustSummary");
  if (trust) {
    const acc = out?.accuracy_rate;
    if (acc == null) trust.textContent = "Trust signal unavailable for this window (insufficient comparable outcomes).";
    else trust.textContent = `This recommendation style matched similar historical outcomes ${Math.round(acc * 100)}% of the time in this replay window.`;
  }

  btState.runs = out?.runs || [];
  btState.selectedId = null;
  renderBacktestTable();
  drawBacktestTimeline();
}

function renderBacktestTable() {
  const tbody = $("btTableBody");
  if (!btState.runs.length) {
    tbody.innerHTML = `<tr><td colspan="11" class="bt-empty">No events in this window.</td></tr>`;
    return;
  }
  const rows = [...btState.runs].sort((a, b) => {
    const av = a[btState.sortKey] ?? 0;
    const bv = b[btState.sortKey] ?? 0;
    const cmp = av < bv ? -1 : av > bv ? 1 : 0;
    return btState.sortDir === "asc" ? cmp : -cmp;
  });
  tbody.innerHTML = rows.map((r) => {
    const flag = r.disagreement
      ? `<span class="bt-flag-yes">FLAGGED</span>`
      : `<span class="bt-flag-no">ok</span>`;
    const when = r.event_ts ? new Date(r.event_ts).toISOString().replace("T", " ").slice(0, 16) : "-";
    const delta = r.confidence_delta != null ? r.confidence_delta.toFixed(3) : "-";
    const hyp = r.hypothesis_confidence != null ? r.hypothesis_confidence.toFixed(2) : "-";
    const rec = r.reconciled_confidence != null ? r.reconciled_confidence.toFixed(2) : "-";
    const predProb = r.predicted_disruption_prob != null ? `${Math.round(Number(r.predicted_disruption_prob) * 100)}%` : "-";
    const predOutcome = r.predicted_outcome || "-";
    const actualOutcome = r.actual_outcome || "-";
    const match = r.matched === true
      ? `<span class="bt-flag-no">MATCH</span>`
      : r.matched === false
        ? `<span class="bt-flag-yes">MISS</span>`
        : `<span class="bt-flag-no">n/a</span>`;
    const sel = btState.selectedId === r.structured_event_id ? "selected" : "";
    return `
      <tr class="${sel}" data-eid="${r.structured_event_id}">
        <td>${escapeHtml(when)}</td>
        <td>${escapeHtml(r.action_type || "-")}</td>
        <td>${escapeHtml(r.target || "-")}</td>
        <td>${hyp}</td>
        <td>${rec}</td>
        <td title="${escapeHtml(predOutcome)}">${escapeHtml(predProb)}</td>
        <td>${escapeHtml(actualOutcome)}</td>
        <td>${match}</td>
        <td>${delta}</td>
        <td>${flag}</td>
        <td><button class="bt-open-btn" type="button">Open</button></td>
      </tr>`;
  }).join("");
  // Header sort indicator
  const ths = $("btTable").querySelectorAll("thead th");
  ths.forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.sort === btState.sortKey) {
      th.classList.add(btState.sortDir === "asc" ? "sort-asc" : "sort-desc");
    }
  });
}

function drawBacktestTimeline() {
  const canvas = $("btTimeline");
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const runs = btState.runs.filter((r) => r.event_ts != null);
  if (!runs.length) {
    ctx.fillStyle = "#51675f";
    ctx.font = "12px 'IBM Plex Sans', sans-serif";
    ctx.fillText("No events with timestamps to plot.", 20, 24);
    return;
  }

  const padL = 44, padR = 12, padT = 14, padB = 24;
  const w = canvas.width - padL - padR;
  const h = canvas.height - padT - padB;

  // Frame + gridlines
  ctx.strokeStyle = "#17272f";
  ctx.strokeRect(padL, padT, w, h);
  ctx.font = "10px 'IBM Plex Sans', sans-serif";
  ctx.fillStyle = "#51675f";
  ctx.textAlign = "right";
  for (let i = 0; i <= 4; i++) {
    const v = i / 4;
    const y = padT + h - v * h;
    ctx.fillText(v.toFixed(2), padL - 6, y + 3);
    ctx.strokeStyle = "rgba(30,47,56,0.55)";
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(padL + w, y);
    ctx.stroke();
    ctx.strokeStyle = "#17272f";
  }
  ctx.textAlign = "start";

  const times = runs.map((r) => Date.parse(r.event_ts));
  const tMin = Math.min(...times);
  const tMax = Math.max(...times);
  const range = Math.max(tMax - tMin, 1);
  const xFor = (t) => padL + ((t - tMin) / range) * w;
  const yFor = (v) => padT + h - Math.max(0, Math.min(1, v || 0)) * h;

  // X-axis time labels (start, mid, end)
  ctx.fillStyle = "#86a8b1";
  ctx.textAlign = "center";
  const fmtDate = (t) => new Date(t).toISOString().slice(0, 10);
  ctx.fillText(fmtDate(tMin), padL, padT + h + 14);
  ctx.fillText(fmtDate(tMin + range / 2), padL + w / 2, padT + h + 14);
  ctx.fillText(fmtDate(tMax), padL + w, padT + h + 14);
  ctx.textAlign = "start";

  // Connecting lines: hypothesis (dim green) + reconciled (teal)
  const drawLine = (getY, color) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    runs.forEach((r, i) => {
      const x = xFor(Date.parse(r.event_ts));
      const y = yFor(getY(r));
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  };
  drawLine((r) => r.hypothesis_confidence, "rgba(34,197,94,0.65)");
  drawLine((r) => r.reconciled_confidence, "rgba(20,184,166,0.85)");

  // Dots
  runs.forEach((r) => {
    const x = xFor(Date.parse(r.event_ts));
    if (r.hypothesis_confidence != null) {
      ctx.fillStyle = "rgba(34,197,94,0.9)";
      ctx.beginPath();
      ctx.arc(x, yFor(r.hypothesis_confidence), 2.5, 0, Math.PI * 2);
      ctx.fill();
    }
    if (r.reconciled_confidence != null) {
      ctx.fillStyle = "#14b8a6";
      ctx.beginPath();
      ctx.arc(x, yFor(r.reconciled_confidence), 3, 0, Math.PI * 2);
      ctx.fill();
    }
    if (r.disagreement) {
      ctx.fillStyle = "#ff8fa1";
      ctx.beginPath();
      ctx.arc(x, padT + 6, 3.4, 0, Math.PI * 2);
      ctx.fill();
    }
  });
}

// Preset ranges  (returns [startISOlocal, endISOlocal] suitable for datetime-local)
function computePresetRange(preset) {
  const now = new Date();
  const end = new Date(now);
  const start = new Date(now);
  switch (preset) {
    case "24h": start.setUTCHours(start.getUTCHours() - 24); break;
    case "7d":  start.setUTCDate(start.getUTCDate() - 7);   break;
    case "30d": start.setUTCDate(start.getUTCDate() - 30);  break;
    case "90d": start.setUTCDate(start.getUTCDate() - 90);  break;
    case "ytd": start.setUTCMonth(0); start.setUTCDate(1); start.setUTCHours(0, 0, 0, 0); break;
    default: return [null, null];
  }
  return [toDatetimeLocalValue(start), toDatetimeLocalValue(end)];
}

function toDatetimeLocalValue(d) {
  // Strip seconds+timezone; format as YYYY-MM-DDTHH:MM for datetime-local inputs.
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function setDashboardLoadingState() {
  ["exRisk", "exConf", "exDriver", "exIndia", "exAction", "exSpr", "exEvidenceQuality",
   "mcMostLikely", "mcRange", "mcExpectedDuration",
   "sprStatus", "sprCoverageDays", "sprRecommendation",
   "refHealthy", "refWatch", "refCritical", "refAffectedCount",
   "procDecisionScore", "procSelectedCount", "procRejectedCount", "exOutcome",
    "opExpectedEffect", "opMonitor", "opTrigger", "opReplan",
    "rtRule", "rtEvidence", "rtSimulation", "rtOptimization", "rtDecision",
    "cdEvidence", "cdReasoning", "cdSimulation", "cdFinal",
    "cdEvidenceWhy", "cdReasoningWhy", "cdSimulationWhy", "cdFinalWhy",
   "outSavings", "outContinuity", "outCoverage",
    "missionObjective", "missionThroughput", "missionStatus", "missionRisk", "missionCoverage",
    "wsTwinStatus", "wsSyncAgo", "wsSources", "wsEvents", "wsConfidence",
    "topStatus", "topReadiness", "topTwinSync"].forEach((id) => {
    const el = $(id);
    if (el) el.textContent = "Loading...";
  });
  renderTriggerExplainability(null);
  setPipelineProgress("collect");
  const confWhy = $("exConfWhy");
  if (confWhy) confWhy.innerHTML = "<li>Refreshing live evidence...</li>";
  const quality = $("exQualityDetail");
  if (quality) quality.textContent = "Refreshing evidence mix...";
  const sev = $("exSeverityNote");
  if (sev) sev.textContent = "Recomputing operational severity...";
  const care = $("careBridge");
  if (care) care.textContent = "Computing signal-to-business impact bridge...";
  const whyNow = $("whyNowText");
  if (whyNow) whyNow.textContent = "Computing why-now catalyst and historical context...";
  const cap = $("mapStoryCaption");
  if (cap) cap.textContent = "Story mode ready when pipeline data loads.";
}

function applyBacktestPreset(preset) {
  document.querySelectorAll("#btPresets button").forEach((b) => b.classList.toggle("active", b.dataset.preset === preset));
  if (preset === "custom") return;
  const [s, e] = computePresetRange(preset);
  if (s && e) {
    $("btStart").value = s;
    $("btEnd").value = e;
  }
}

async function refreshPanels(options = {}) {
  const quick = !!options.quick;
  const force = !!options.force;
  const btn = $("triggerBtn");
  if (btn) btn.dataset.state = "running";
  setDashboardLoadingState();
  setPipelineProgress("collect");
  setStatus("Updating Intelligence...");
  try {
    const missionObjective = inferMissionObjective();
    const importBudget = getImportBudgetUsdBn();
    const qs = new URLSearchParams();
    if (quick) qs.set("quick", "true");
    if (force) qs.set("force", "true");
    qs.set("mission_objective", missionObjective);
    if (importBudget !== null) qs.set("annual_import_budget_usd_bn", String(importBudget));
    const refreshUrl = `/pipeline/refresh${qs.toString() ? `?${qs.toString()}` : ""}`;
    setPipelineProgress("normalize");
    // Call the full ingest → extract → pipeline endpoint
    const result = await apiFetch(refreshUrl, {
      method: "POST",
      timeoutMs: quick ? 120000 : 240000,
    });
    setPipelineProgress("twin");
    lastRefreshSelection = result.selected_event || null;
    renderTriggerExplainability(lastRefreshSelection, result.selection_mode || "");
    // Store the new IDs so subsequent detail-loads and scenario runs work
    if (result.pipeline_id) $("pipelineId").value = result.pipeline_id;
    if (result.structured_event_id) $("eventId").value = result.structured_event_id;
    setPipelineProgress("reasoning");
    // Pull fresh pipeline details + repaint all panels
    await refreshPipelineDetails();
    // Also refresh twin state (chokepoints/routes may have changed)
    await hydrateMapFromTwin(true);
    if (mapLayersState.ais) {
      await ensureLiveAisPoints(true);
      refreshDeckLayers();
    }
    await ensureEvidenceCaches();
    renderReasoningEvidence();
    setPipelineProgress("recommendation");
    completePipelineProgress();
    if (btn) btn.dataset.state = "on";
    const note = [
      result.ingested_new ? `+${result.ingested_new} signals` : "",
      result.extracted_new ? `+${result.extracted_new} events` : "",
    ].filter(Boolean).join(", ");
    const freshnessLabel = (() => {
      const mode = result.selection_mode || "";
      if (result.reused_latest) return "reused cached run";
      if (mode === "latest") return "latest event";
      if (mode === "rotated_recent") return "rotated to a different recent event";
      if (mode === "distinct_recent") return "picked a distinct recent event";
      if (mode === "reused_latest_no_significant_news") return "no significant new event · reused latest coherent run";
      return mode ? `mode=${mode}` : "";
    })();
    const eventPickLabel = (() => {
      const sel = result.selected_event || {};
      if (!sel.source && !sel.age_minutes) return "";
      const age = Number.isFinite(Number(sel.age_minutes)) ? `${sel.age_minutes}m` : "n/a";
      return `event=${sel.source || "unknown"} (${age})`;
    })();
    setStatus("Pipeline Ready", "ok");
    // Refresh the market ticker too
    loadMarketTicker().catch(() => {});
    initLatestEvent().catch(() => {});
    loadTwinSummary().catch(() => {});
  } catch (err) {
    if (btn) btn.dataset.state = "err";
    markPipelineAttention();
    const topStatus = $("topStatus");
    const topReadiness = $("topReadiness");
    if (topStatus) topStatus.textContent = "Degraded";
    if (topReadiness) topReadiness.textContent = "Blocked";
    setStatus(err.message, "err");
  }
}

async function loadTwinSummary() {
  try {
    twinSummaryCache = await apiFetch("/digital-twin/summary");
    renderFreshnessStrip();
    if (lastPipelineDetails) updateWorldStateFooter(lastPipelineDetails);
  } catch {
    // best-effort only
  }
}

function startGuidedDemo() {
  const steps = [
    ["Situation", "mapCard"],
    ["Reasoning", "reasoningCard"],
    ["Forecast", "forecastCard"],
    ["Recommended actions", "procurementCard"],
    ["Validation", "validationCard"],
  ];
  setStatus("guided demo started", "ok");
  document.body.classList.add("demo-focus");
  steps.forEach(([label, id], idx) => {
    setTimeout(() => {
      flashAndScrollTo(id);
      setStatus(`guided demo · ${label}`, "ok");
    }, idx * 2400);
  });
}

function runTrustPathStep(step) {
  if (step === "evidence") {
    const node = pickIncidentNodeFromContext();
    if (node) {
      showIncidents(node).catch((err) => setStatus(err.message, "err"));
    }
    flashAndScrollTo("mapCard");
    setStatus("trust path · evidence", "ok");
    return;
  }

  if (step === "reasoning") {
    flashAndScrollTo("reasoningCard");
    setStatus("trust path · reasoning", "ok");
    return;
  }

  if (step === "counter") {
    flashAndScrollTo("mapCard");
    const rebuttal = $("rebuttalCard");
    if (rebuttal) {
      rebuttal.classList.remove("flash-target");
      void rebuttal.offsetWidth;
      rebuttal.classList.add("flash-target");
    }
    setStatus("trust path · counter-reasoning", "ok");
    return;
  }

  if (step === "decision") {
    flashAndScrollTo("procurementCard");
    setStatus("trust path · decision", "ok");
    return;
  }

  if (step === "validation") {
    const bt = $("btDetails");
    if (bt) bt.open = true;
    flashAndScrollTo("validationCard");
    setStatus("trust path · validation", "ok");
  }
}

$("triggerBtn").addEventListener("click", () => triggerPipeline().catch((e) => setStatus(e.message, "err")));
$("entityBtn").addEventListener("click", () => showIncidents().catch((e) => setStatus(e.message, "err")));
$("entitySelect").addEventListener("change", (e) => {
  const v = e.target.value;
  if (v) showIncidents(v).catch((err) => setStatus(err.message, "err"));
});
$("reasoningEvidence").addEventListener("click", (e) => {
  const btn = e.target.closest("button.ev-chip-btn[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  if (action === "focus-map") {
    fitMapToCausalChain(lastPipelineDetails?.hypothesis?.causal_chain);
    flashAndScrollTo("mapCard");
    return;
  }
  if (action === "scroll-forecast") {
    const mcDetails = $("mcWhyDetails");
    if (mcDetails) mcDetails.open = true;
    flashAndScrollTo("forecastCard");
    return;
  }
  if (action === "scroll-actions") {
    flashAndScrollTo("procurementCard");
    return;
  }
  if (action === "scroll-map") {
    const node = pickIncidentNodeFromContext();
    if (node) {
      showIncidents(node).catch((err) => setStatus(err.message, "err"));
    }
    flashAndScrollTo("mapCard");
  }
});
$("guidedDemoBtn").addEventListener("click", startGuidedDemo);
$("explainEventBtn")?.addEventListener("click", () => {
  startEventStoryMode().catch((e) => setStatus(e.message, "err"));
});
$("playPropagationBtn")?.addEventListener("click", () => {
  const chain = lastPipelineDetails?.hypothesis?.causal_chain;
  if (!chain?.steps?.length) {
    setStatus("load a pipeline with causal chain first", "err");
    return;
  }
  playCausalPropagation(chain, { updateCaption: true, perStepMs: 760 }).catch((e) => setStatus(e.message, "err"));
});
$("decisionModeToggle")?.addEventListener("change", (e) => {
  setDecisionMode(!!e.target.checked);
  const label = $("mapModeLabel");
  if (label) {
    if (e.target.checked) label.textContent = "Mode: decision";
    else if (lastPipelineDetails) label.textContent = `Mode: ${inferEventContextType(lastPipelineDetails)}`;
    else label.textContent = "Mode: adaptive";
  }
});
$("traceEvidenceBtn").addEventListener("click", () => {
  flashAndScrollTo("reasoningCard");
});
$("trustPathCard")?.addEventListener("click", (e) => {
  const btn = e.target.closest("button.trust-step[data-step]");
  if (!btn) return;
  runTrustPathStep(btn.dataset.step);
});
$("recommendationTrace")?.addEventListener("click", (e) => {
  const btn = e.target.closest("button.trace-step[data-step]");
  if (!btn) return;
  const step = btn.dataset.step;

  if (step === "rule") {
    runTrustPathStep("reasoning");
    setStatus("trace path · rule", "ok");
    return;
  }

  if (step === "evidence") {
    runTrustPathStep("evidence");
    setStatus("trace path · evidence", "ok");
    return;
  }

  if (step === "simulation") {
    const mcDetails = $("mcWhyDetails");
    if (mcDetails) mcDetails.open = true;
    flashAndScrollTo("forecastCard");
    setStatus("trace path · simulation", "ok");
    return;
  }

  if (step === "optimization") {
    flashAndScrollTo("procurementCard");
    setStatus("trace path · optimization", "ok");
    return;
  }

  runTrustPathStep("decision");
  setStatus("trace path · decision", "ok");
});
$("btBtn").addEventListener("click", () => runBacktest().catch((e) => setStatus(e.message, "err")));
$("btRunPrimary").addEventListener("click", () => runBacktest().catch((e) => setStatus(e.message, "err")));
$("refreshBtn").addEventListener("click", () => refreshPanels({ quick: true, force: true }).catch((e) => setStatus(e.message, "err")));
$("scenarioBtn").addEventListener("click", () => runScenarioBranch().catch((e) => setStatus(e.message, "err")));
$("scenarioClearBtn").addEventListener("click", clearScenarioBranch);
$("scenarioSelect").addEventListener("change", updateScenarioHint);
$("eventSpotlight")?.addEventListener("click", () => {
  const node = $("spotNode")?.textContent || "";
  if (node) focusMapOnEntity(node);
  flashAndScrollTo("reasoningCard");
});

$("missionEditBtn")?.addEventListener("click", () => {
  const el = $("missionObjective");
  if (!el) return;
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel?.removeAllRanges();
  sel?.addRange(range);
});

// Backtest presets — clicking sets both datetime inputs and marks active pill.
$("btPresets").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-preset]");
  if (!btn) return;
  applyBacktestPreset(btn.dataset.preset);
});

// Backtest table: sort by column, click a row to load its pipeline into the
// main dashboard.
$("btTable").addEventListener("click", (e) => {
  const th = e.target.closest("thead th[data-sort]");
  if (th) {
    const key = th.dataset.sort;
    if (btState.sortKey === key) {
      btState.sortDir = btState.sortDir === "asc" ? "desc" : "asc";
    } else {
      btState.sortKey = key;
      btState.sortDir = key === "event_ts" ? "desc" : "asc";
    }
    renderBacktestTable();
    return;
  }
  const row = e.target.closest("tbody tr[data-eid]");
  if (!row) return;
  const eid = Number(row.dataset.eid);
  if (!eid) return;
  btState.selectedId = eid;
  renderBacktestTable();
  // Trigger a fresh pipeline for that event so all cards populate.
  $("eventId").value = eid;
  triggerPipeline().catch((err) => setStatus(err.message, "err"));
});

// Default preset on page load: YTD (more likely to include validation signal).
applyBacktestPreset("ytd");

setStatus("Preparing Dashboard...");
updateStatusTheme("idle");
$("triggerBtn").dataset.state = "off";
bindMonteCarloToolbar();
initMap();
initMapLayerControls();
bindMissionObjectiveEditor();
bindImportBudgetInput();
loadScenarioPresets().catch(() => {});
loadTwinSummary().catch(() => {});
loadMarketTicker().catch(() => {});
initLatestEvent().catch(() => {});
ensureLiveAisPoints().then(() => refreshDeckLayers()).catch(() => {});

// First-load warm start: auto-refresh so users land on a current pipeline.
(async () => {
  try {
    await refreshPanels({ quick: true });
  } catch (err) {
    console.warn("initial warm refresh failed", err);
    setStatus("ready (warm start failed — use Refresh Panels)", "idle");
  }
})();
