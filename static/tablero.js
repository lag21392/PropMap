const statusEl = document.getElementById("boardStatus");
let timer = 0;
let hurry = true;
let fetchCtrl = null;

const LANE = {
  local: { label: "IP local", color: "#2a4a3c" },
  tor: { label: "Tor", color: "#9a5a28" },
  vpn: { label: "VPN / proxy", color: "#3d6f8a" },
  translate: { label: "Traductor", color: "#6a4c8a" },
  stealth: { label: "Stealth", color: "#8a3d5c" },
  otros: { label: "Otros", color: "#6a8f2e" },
};
const LANE_ORDER = ["local", "tor", "vpn", "translate", "stealth", "otros"];

function fmt(n) {
  const v = Number(n || 0);
  return Number.isFinite(v) ? v.toLocaleString("es-AR") : "0";
}

function laneKind(id, kind) {
  if (kind === "direct" || id === "direct" || id === "urllib") return "local";
  if (kind === "tor" || id === "tor" || String(id).startsWith("tor")) return "tor";
  if (kind === "proxy" || String(id).startsWith("proxy")) return "vpn";
  if (kind === "translate" || id === "translate") return "translate";
  if (kind === "stealth" || id === "stealth") return "stealth";
  return "otros";
}

function laneTitle(id, kind) {
  const name = laneKind(id, kind);
  if (name === "tor") {
    const bits = String(id).match(/(\d+)$/);
    return bits ? `Tor ${Number(bits[1]) + 1}` : "Tor";
  }
  return (LANE[name] || LANE.otros).label;
}

function hostShort(host) {
  return String(host || "").replace(/^www\./, "");
}

function waitCopy(seconds) {
  const s = Number(seconds || 0);
  if (s < 1) return "libre";
  if (s < 90) return `espera ${Math.round(s)} s`;
  return `pausa ${Math.round(s / 60)} min`;
}

function kpi(label, value) {
  return `<div><dt>${label}</dt><dd>${value}</dd></div>`;
}

function esc(text) {
  return String(text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function joinEs(parts) {
  if (parts.length <= 1) return parts[0] || "";
  if (parts.length === 2) return `${parts[0]} y ${parts[1]}`;
  return `${parts.slice(0, -1).join(", ")} y ${parts[parts.length - 1]}`;
}

async function load() {
  if (fetchCtrl) fetchCtrl.abort();
  const ctrl = new AbortController();
  fetchCtrl = ctrl;
  const kill = setTimeout(() => ctrl.abort(), 3500);
  try {
    const res = await fetch("/api/ops", { credentials: "same-origin", signal: ctrl.signal });
    if (res.status === 401) {
      location.assign("/tablero");
      return;
    }
    if (!res.ok) throw new Error(String(res.status));
    paint(await res.json());
  } finally {
    clearTimeout(kill);
    if (fetchCtrl === ctrl) fetchCtrl = null;
  }
}

function paint(data) {
  statusEl.hidden = true;
  const live = data.live || {};
  const tel = data.telemetry || {};
  const inv = data.inventory || {};
  const egress = data.egress || {};
  const move = data.movement || {};
  const llm = data.llm_pipe || {};
  const fichas = data.details_pipe || {};
  const running = Boolean(live.running);
  const cities = (live.running_cities || []).join(", ") || "nadie";
  const now = document.getElementById("nowStrip");
  now.hidden = false;
  document.getElementById("nowLead").textContent = running
    ? `Scrape en curso · ${cities} · ${live.mode || "pasada"} · ${live.message || "trabajando"}`
    : live.message && live.message !== "Todavía no se buscó nada."
      ? live.message
      : "El motor rota lugares sin parar. Enseguida arranca el próximo.";

  paintLanes(egress.tracks || [], tel, data.busy || [], egress);
  paintMove(move, tel.series || []);
  paintLlm(llm, tel.series || []);
  paintFichas(fichas, tel.series || []);
  paintHttp(tel.http || {});
  paintStock(inv, tel);
  paintCities(inv.cities || []);
  paintLogs(live.logs || []);
  paintVisits(data.visits || []);
  hurry = running
    || Number(llm.working || 0) > 0
    || Number(llm.queue || 0) > 0
    || Number(llm.this_min || 0) > 0
    || Number(fichas.working || 0) > 0
    || Number(fichas.queue || 0) > 0;
}

function paintLanes(tracks, tel, busy, egress) {
  const host = document.getElementById("lanesBlock");
  const box = document.getElementById("tracks");
  host.hidden = false;
  const groups = (tel.http || {}).by_group || {};
  const byLane = (tel.http || {}).by_lane || {};
  const latest = latestLanes(tel.series || []);
  const lastLanes = latest.lanes;
  const mixLabel = latest.ago === 0 ? "este minuto" : latest.ago === 1 ? "hace 1 min" : `hace ${latest.ago} min`;
  const busyBy = {};
  for (const row of busy) {
    (busyBy[row.lane] || (busyBy[row.lane] = [])).push(row);
  }
  const active = (busy || []).filter((row) => !row.cooling);
  const activeKinds = [...new Set(active.map((row) => laneKind(row.lane)))];
  const nowLabels = activeKinds.map((kind) => LANE[kind].label);
  const coolingN = (busy || []).filter((row) => row.cooling).length;
  const mixKeys = LANE_ORDER.filter((k) => Number(lastLanes[k] || 0) > 0);
  const lastUsed = LANE_ORDER.filter((k) => lastLanes[k]).map((k) => LANE[k].label);
  const extra = Number((egress || {}).tor_extra || 0);
  const circuits = Number((egress || {}).tor_circuits || 0);
  const extraNote = extra
    ? ` Abrió ${fmt(extra)} circuitos Tor extra (${fmt(circuits)} en total).`
    : circuits
      ? ` Tor: ${fmt(circuits)} circuitos.`
      : "";
  document.getElementById("laneNow").textContent = nowLabels.length
    ? `Ahora sale por ${joinEs(nowLabels)}.${extraNote}`
    : coolingN
      ? `Carriles en pausa.${lastUsed.length ? ` El último tráfico salió por ${joinEs(lastUsed)}.` : ""}${extraNote}`
      : groups.local || groups.tor || groups.vpn || groups.translate || groups.stealth || groups.otros
        ? `En este proceso (desde el último reinicio): ${LANE_ORDER.filter((k) => groups[k]).map((k) => `${LANE[k].label} ${fmt(groups[k])}`).join(" · ")}.${extraNote}`
        : `Todavía no hubo tráfico en este proceso.${extraNote}`;
  const mix = document.getElementById("laneMix");
  if (mixKeys.length) {
    mix.hidden = false;
    mix.innerHTML = mixKeys
      .map((key) => `<li><i style="background:${LANE[key].color}"></i><b>${fmt(lastLanes[key] || 0)}</b> ${LANE[key].label} · ${mixLabel}</li>`)
      .join("");
  } else {
    mix.hidden = true;
    mix.innerHTML = "";
  }

  if (!tracks.length) {
    box.innerHTML = "<p class='tablero-empty'>No hay carriles configurados.</p>";
  } else {
    box.innerHTML = tracks
      .map((track) => {
        const kind = laneKind(track.id, track.kind);
        const wait = Math.max(
          Number(track.down_s || 0),
          ...((busyBy[track.id] || []).map((row) => Number(row.wait_s || 0))),
        );
        const cooling = (busyBy[track.id] || []).some((row) => row.cooling) || Number(track.down_s || 0) > 8;
        const hits = byLane[track.id] || 0;
        const hosts = (busyBy[track.id] || [])
          .map((row) => `${esc(hostShort(row.host))} ${waitCopy(row.wait_s)}`)
          .join(" · ");
        const state = Number(track.down_s || 0) > 0
          ? "caído un rato"
          : cooling
            ? "en pausa"
            : wait > 0.2
              ? "ocupado"
              : "libre";
        const using = active.some((row) => row.lane === track.id);
        return `<article class="tablero-track is-${kind} ${cooling ? "is-cool" : ""} ${using ? "is-now" : ""}">
          <header>
            <strong>${laneTitle(track.id, track.kind)}</strong>
            <span>${using ? "en uso" : state}</span>
          </header>
          <p class="tablero-mono">${fmt(hits)} requests · ${waitCopy(wait)}${hosts ? ` · ${hosts}` : ""}</p>
        </article>`;
      })
      .join("");
  }
  const keys = LANE_ORDER.filter((k) => (tel.series || []).some((row) => Number((row.lanes || {})[k] || 0) > 0) || groups[k]);
  const fallback = tracks.some((track) => track.kind === "tor") ? "tor" : "local";
  const useKeys = keys.length ? keys : [fallback];
  stackedArea(document.getElementById("laneChart"), tel.series || [], useKeys, (key) => LANE[key].color);
  document.getElementById("laneLegend").innerHTML = useKeys
    .map((key) => `<li><i style="background:${LANE[key].color}"></i>${LANE[key].label} · ${fmt(groups[key] || 0)}</li>`)
    .join("");
}

function paintMove(move, series) {
  document.getElementById("moveBlock").hidden = false;
  const neuToday = Number(move.new || 0);
  const goneToday = Number(move.gone || 0);
  const seenToday = Number(move.seen || 0);
  const neu24 = Number(move.new_24h || 0);
  const gone24 = Number(move.gone_24h || 0);
  const seen24 = Number(move.seen_24h || 0);
  const passNew = Number(move.pass_new || 0);
  const today = move.scope === "today";
  const use24 = !(neuToday || goneToday || seenToday) && (neu24 || gone24 || seen24);
  const neu = use24 ? neu24 : neuToday;
  const gone = use24 ? gone24 : goneToday;
  const seen = use24 ? seen24 : seenToday;
  const net = neu - gone;
  const when = use24 ? "Últimas 24 h" : today ? "Hoy" : "Este proceso";
  if (neu || gone) {
    const extra = use24 && today ? " Hoy todavía no cerró ninguna pasada." : "";
    document.getElementById("moveLead").textContent =
      `${when}: ${fmt(neu)} nuevas, ${fmt(gone)} desaparecieron. Neto ${net >= 0 ? "+" : ""}${fmt(net)}.${extra}`;
  } else if (seen) {
    document.getElementById("moveLead").textContent =
      `${when} el scrape revisó ${fmt(seen)} avisos que ya estaban. Todavía no apareció ninguno nuevo ni se dio de baja otro.`;
  } else {
    document.getElementById("moveLead").textContent =
      "Cuando un portal termine una pasada vas a ver altas y bajas acá. Se guardan en la base, no se pierden al reiniciar.";
  }
  document.getElementById("moveKpis").innerHTML = [
    kpi(use24 ? "Nuevas 24 h" : "Nuevas", fmt(neu)),
    kpi(use24 ? "Bajas 24 h" : "Desaparecieron", fmt(gone)),
    kpi("Neto", `${net >= 0 ? "+" : ""}${fmt(net)}`),
    kpi(use24 ? "Vistos 24 h" : "Vistos", fmt(seen)),
    kpi("Esta pasada", fmt(passNew)),
  ].join("");
  ledgerChart(document.getElementById("moveChart"), series);
}

function paceLabel(perHour, pipe) {
  if (!perHour) return "—";
  const n = fmt(Math.round(perHour));
  if (pipe.rate_scope === "day") return `${n} (hoy)`;
  if (pipe.held) return `${n} (sostiene)`;
  return n;
}

function etaCopy(hours, need, live) {
  if (!need) return "";
  const h = Number(hours);
  if (!Number.isFinite(h) || h <= 0 || h > 240) {
    return live
      ? "Está procesando; el ritmo de esta sesión aparece en cuanto termine el primer aviso."
      : "Todavía no hay ritmo para estimar cuánto falta.";
  }
  if (h < 1.5) return `A este ritmo, unos ${Math.max(1, Math.round(h * 60))} min para vaciar lo que falta.`;
  if (h < 40) return `A este ritmo, unas ${Math.round(h)} h para vaciar lo que falta.`;
  return `A este ritmo, unos ${(h / 24).toFixed(1)} días para vaciar lo que falta.`;
}

function paintLlm(pipe, series) {
  document.getElementById("llmBlock").hidden = false;
  const done = Number(pipe.done || 0);
  const need = Number(pipe.need || 0);
  const queue = Number(pipe.queue || 0);
  const working = Number(pipe.working || 0);
  const partial = Number(pipe.partial || 0);
  const awaitDir = Number(pipe.await_dir || 0);
  const workers = Number(pipe.workers || 0);
  const cap = Number(pipe.cap || 0);
  const perHour = Number(pipe.per_hour || 0);
  const thisMin = Number(pipe.this_min || 0);
  const okH = Number(pipe.ok_h || 0);
  const okD = Number(pipe.ok_d || 0);
  const failH = Number(pipe.fail_h || 0);
  const gpu = Boolean(pipe.gpu);
  const llamaOk = pipe.llama_ok !== false;
  const busyS = Number(pipe.busy_s || 0);
  const ok24 = Number(pipe.ok_24 || 0);
  const stuck = Boolean(working) && !gpu && busyS > 20;
  const nowBits = [];
  if (gpu) {
    nowBits.push(`GPU generando${busyS ? ` hace ${Math.round(busyS)} s` : ""}`);
  } else if (stuck) {
    nowBits.push(`GPU quieta · el worker se trabó ${Math.round(busyS)} s en un aviso`);
  } else if (queue && working) {
    nowBits.push(`preparando el próximo aviso${busyS ? ` (${Math.round(busyS)} s)` : ""}`);
  } else if (queue) {
    nowBits.push("GPU libre y hay cola: debería arrancar ya");
  } else {
    nowBits.push("GPU libre");
  }
  if (!llamaOk) nowBits.push("llama.cpp no responde");
  nowBits.push(`${fmt(queue)} en cola${cap ? ` (tope ${fmt(cap)}, se rellena sola)` : ""}`);
  nowBits.push(`${fmt(thisMin)} listo${thisMin === 1 ? "" : "s"} este minuto`);
  if (perHour && (thisMin || okH || okD || ok24 || pipe.held)) nowBits.push(`~${fmt(Math.round(perHour))}/hora`);
  document.getElementById("llmNow").textContent = `Ahora: ${nowBits.join(" · ")}.`;
  document.getElementById("llmLead").textContent = need
    ? (stuck
        ? `Hay ${fmt(queue)} en cola pero la GPU no está generando. No es que falte trabajo: el pedido a llama.cpp se colgó.`
        : `En la base faltan ${fmt(need)}. ${etaCopy(pipe.eta_h, need, gpu || working || thisMin || okH || okD || ok24)}`)
    : "No hay avisos pendientes de esta versión del LLM.";
  paintStack("llmNowStack", [
    { label: "GPU generando", n: gpu ? 1 : 0, color: "#3d6f8a" },
    { label: "Worker trabado", n: stuck ? 1 : 0, color: "#c45c3a" },
    { label: "En cola", n: queue, color: "#9a5a28" },
    { label: "Listos este minuto", n: thisMin, color: "#2a4a3c" },
  ]);
  paintStack("llmStack", [
    { label: "Listos", n: done, color: "#2a4a3c" },
    { label: "Parciales", n: partial, color: "#6a8f2e" },
    { label: "Faltan", n: need, color: "#a07a2e" },
  ]);
  document.getElementById("llmKpis").innerHTML = [
    kpi("Este minuto", fmt(thisMin)),
    kpi("Última hora", fmt(okH)),
    kpi("Hoy", fmt(okD)),
    kpi("24 h", fmt(ok24)),
    kpi("Por hora", paceLabel(perHour, pipe)),
    kpi("GPU", gpu ? "generando" : queue || working ? "quieta" : "libre"),
    kpi("Worker", busyS ? `${Math.round(busyS)} s` : `${fmt(working)}/${fmt(workers || 1)}`),
    kpi("En cola", fmt(queue)),
    kpi("Errores / h", fmt(failH)),
    kpi("Listos", `${fmt(done)} · ${pipe.pct || 0}%`),
    kpi("Faltan", fmt(need)),
    kpi("Parciales", fmt(partial)),
    kpi("Sin dirección", fmt(awaitDir)),
  ].join("");
  outcomeBars(document.getElementById("llmChart"), series || [], "llm_by");
  document.getElementById("llmChartLegend").innerHTML = [
    ["#2a4a3c", "Listos"],
    ["#6a8f2e", "Parciales"],
    ["#c45c3a", "Errores"],
    ["#9a5a28", "Reintentos"],
  ]
    .map(([color, label]) => `<li><i style="background:${color}"></i>${label}</li>`)
    .join("");
}

function paintFichas(pipe, series) {
  document.getElementById("fichaBlock").hidden = false;
  const done = Number(pipe.done || 0);
  const need = Number(pipe.need || 0);
  const queue = Number(pipe.queue || 0);
  const working = Number(pipe.working || 0);
  const thisMin = Number(pipe.this_min || 0);
  const perHour = Number(pipe.per_hour || 0);
  const okH = Number(pipe.ok_h || 0);
  const okD = Number(pipe.ok_d || 0);
  const workers = Number(pipe.workers || 0);
  document.getElementById("fichaNow").textContent = `Ahora: ${fmt(working)} bajando${workers ? ` / ${fmt(workers)}` : ""} · ${fmt(queue)} en cola · ${fmt(thisMin)} este minuto${perHour && (thisMin || okH || okD || pipe.held) ? ` · ~${fmt(Math.round(perHour))}/h` : ""}.`;
  document.getElementById("fichaLead").textContent = need
    ? `Faltan bajar ${fmt(need)} fichas. ${etaCopy(pipe.eta_h, need, working || thisMin || okH || okD)}`
    : "Todas las fichas de la base ya se bajaron.";
  paintStack("fichaStack", [
    { label: "Bajadas", n: done, color: "#2a4a3c" },
    { label: "Bajando", n: working, color: "#3d6f8a" },
    { label: "En cola", n: queue, color: "#9a5a28" },
    { label: "Faltan", n: Math.max(0, need - queue - working), color: "#a07a2e" },
  ]);
  document.getElementById("fichaKpis").innerHTML = [
    kpi("Bajadas", `${fmt(done)} · ${pipe.pct || 0}%`),
    kpi("Faltan", fmt(need)),
    kpi("En cola", fmt(queue)),
    kpi("Bajando", fmt(working)),
    kpi("Este minuto", fmt(thisMin)),
    kpi("Última hora", fmt(okH)),
    kpi("Hoy", fmt(okD)),
    kpi("Por hora", paceLabel(perHour, pipe)),
  ].join("");
  const chart = document.getElementById("fichaChart");
  if (chart) outcomeBars(chart, series || [], "details_by");
}

function paintStack(id, parts) {
  const el = document.getElementById(id);
  if (!el) return;
  const shown = parts.filter((row) => Number(row.n || 0) > 0);
  const bars = shown.length ? shown : parts;
  const total = bars.reduce((sum, row) => sum + Number(row.n || 0), 0);
  const cols = bars
    .map((row) => `${total ? ((row.n / total) * 100).toFixed(3) : (100 / Math.max(bars.length, 1)).toFixed(3)}%`)
    .join(" ");
  el.innerHTML = `<div class="tablero-stack-bar" style="grid-template-columns:${cols || "100%"}">${bars
    .map((row) => `<i style="background:${row.color}" title="${row.label}"></i>`)
    .join("")}</div><ul class="tablero-legend">${parts
    .map((row) => `<li><i style="background:${row.color}"></i>${row.label} · ${fmt(row.n)}</li>`)
    .join("")}</ul>`;
}

function latestLanes(series) {
  for (let i = series.length - 1, ago = 0; i >= 0 && ago <= 5; i -= 1, ago += 1) {
    const lanes = (series[i] || {}).lanes || {};
    if (LANE_ORDER.some((key) => Number(lanes[key] || 0) > 0)) {
      return { lanes, ago };
    }
  }
  return { lanes: {}, ago: 0 };
}

function clockLabel(ts) {
  const d = new Date(Number(ts) * 1000);
  if (!Number.isFinite(d.getTime())) return "";
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function axisTimes(series, w, pad, y, spanLabel) {
  const cols = Math.max(1, series.length);
  const innerW = w - pad.l - pad.r;
  const x = (i) => pad.l + (cols === 1 ? innerW / 2 : (i / (cols - 1)) * innerW);
  const mid = Math.floor((cols - 1) / 2);
  const left = (series[0] && series[0].t && clockLabel(series[0].t)) || "−24 h";
  const middle = (series[mid] && series[mid].t && clockLabel(series[mid].t)) || "−12 h";
  const marks = spanLabel
    ? [
        [0, spanLabel],
        [cols - 1, "ahora"],
      ]
    : [
        [0, left],
        [mid, middle],
        [cols - 1, "ahora"],
      ];
  return marks
    .map(([i, label]) => {
      const anchor = i === 0 ? "start" : i >= cols - 1 ? "end" : "middle";
      return `<text x="${x(i).toFixed(1)}" y="${y}" text-anchor="${anchor}" fill="#5a6b61" font-size="10" font-family="IBM Plex Mono,monospace">${label}</text>`;
    })
    .join("");
}

function stackedArea(svg, series, keys, colorOf) {
  const w = 720;
  const h = 160;
  const pad = { t: 14, r: 8, b: 22, l: 8 };
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const cols = Math.max(1, series.length);
  const totals = series.map((row) => keys.reduce((sum, key) => sum + Number((row.lanes || {})[key] || 0), 0));
  const max = Math.max(1, ...totals);
  const x = (i) => pad.l + (cols === 1 ? innerW / 2 : (i / (cols - 1)) * innerW);
  const y = (v) => pad.t + innerH - (v / max) * innerH;
  const areas = keys.map((key, idx) => {
    const top = [];
    const bot = [];
    for (let i = 0; i < series.length; i += 1) {
      let base = 0;
      for (let k = 0; k < idx; k += 1) base += Number((series[i].lanes || {})[keys[k]] || 0);
      const val = Number((series[i].lanes || {})[key] || 0);
      top.push(`${x(i).toFixed(1)},${y(base + val).toFixed(1)}`);
      bot.push(`${x(i).toFixed(1)},${y(base).toFixed(1)}`);
    }
    return `<polygon fill="${colorOf(key)}" fill-opacity="0.82" points="${top.join(" ")} ${bot.reverse().join(" ")}" />`;
  });
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.innerHTML = `<rect x="0" y="0" width="${w}" height="${h}" fill="#fbfcf9" />
    <text x="${pad.l}" y="11" fill="#5a6b61" font-size="10" font-family="IBM Plex Mono,monospace">${max}/min</text>
    <line x1="${pad.l}" y1="${pad.t + innerH}" x2="${w - pad.r}" y2="${pad.t + innerH}" stroke="rgba(42,74,60,.2)" />
    ${areas.join("")}
    ${axisTimes(series, w, pad, h - 6)}`;
}

function ledgerChart(svg, series) {
  const w = 720;
  const h = 148;
  const pad = { t: 8, r: 8, b: 18, l: 8 };
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const mid = pad.t + innerH / 2;
  const cols = Math.max(1, series.length);
  const max = Math.max(
    1,
    ...series.map((row) => Math.max(Number(row.new || 0), Number(row.gone || 0))),
  );
    const barW = Math.max(0.45, innerW / cols - 0.05);
  const bars = series.map((row, i) => {
    const cx = pad.l + (i + 0.5) * (innerW / cols);
    const up = Number(row.new || 0);
    const down = Number(row.gone || 0);
    const uh = (up / max) * (innerH / 2 - 2);
    const dh = (down / max) * (innerH / 2 - 2);
    return `${up ? `<rect x="${(cx - barW / 2).toFixed(1)}" y="${(mid - uh).toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0.6, uh).toFixed(1)}" fill="#1f7a4a" />` : ""}
      ${down ? `<rect x="${(cx - barW / 2).toFixed(1)}" y="${mid.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0.6, dh).toFixed(1)}" fill="#c45c3a" />` : ""}`;
  });
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.innerHTML = `<rect x="0" y="0" width="${w}" height="${h}" fill="#fbfcf9" />
    <line x1="${pad.l}" y1="${mid}" x2="${w - pad.r}" y2="${mid}" stroke="rgba(42,74,60,.35)" />
    ${bars.join("")}
    ${axisTimes(series, w, pad, h - 5)}`;
}

function outcomeBars(svg, series, field) {
  if (!svg) return;
  const w = 720;
  const h = 148;
  const pad = { t: 16, r: 8, b: 22, l: 8 };
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const cols = Math.max(1, series.length);
  const colors = { ok: "#2a4a3c", partial: "#6a8f2e", fail: "#c45c3a", retry: "#9a5a28" };
  const keys = ["ok", "partial", "fail", "retry"];
  const values = series.map((row) => {
    const by = row[field] || {};
    const out = {
      ok: Number(by.ok || 0),
      partial: Number(by.partial || 0),
      fail: Number(by.fail || 0),
      retry: Number(by.retry || 0),
    };
    if (field === "details_by" && !out.ok && !out.fail && Number(row.details || 0)) {
      out.ok = Number(row.details || 0);
    }
    return out;
  });
  const totals = values.map((row) => keys.reduce((sum, key) => sum + row[key], 0));
  const max = Math.max(1, ...totals);
  const barW = Math.max(0.45, innerW / cols - 0.05);
  const bars = values.map((row, i) => {
    const cx = pad.l + (i + 0.5) * (innerW / cols);
    let y = pad.t + innerH;
    return keys
      .map((key) => {
        const n = row[key];
        if (!n) return "";
        const hgt = Math.max(3.5, (n / max) * innerH);
        y -= hgt;
        return `<rect x="${(cx - barW / 2).toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${hgt.toFixed(1)}" fill="${colors[key]}" />`;
      })
      .join("");
  });
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.innerHTML = `<rect x="0" y="0" width="${w}" height="${h}" fill="#fbfcf9" />
    <text x="${pad.l}" y="12" fill="#5a6b61" font-size="10" font-family="IBM Plex Mono,monospace">${max}/min</text>
    <line x1="${pad.l}" y1="${pad.t + innerH}" x2="${w - pad.r}" y2="${pad.t + innerH}" stroke="rgba(42,74,60,.2)" />
    ${bars.join("")}
    ${axisTimes(series, w, pad, h - 6)}`;
}

function lineChart(svg, series, pick, color) {
  const w = 720;
  const h = 96;
  const pad = { t: 10, r: 8, b: 18, l: 8 };
  const innerW = w - pad.l - pad.r;
  const innerH = h - pad.t - pad.b;
  const cols = Math.max(1, series.length);
  const vals = series.map(pick);
  const max = Math.max(1, ...vals);
  const pts = vals.map((n, i) => {
    const x = pad.l + (cols === 1 ? innerW / 2 : (i / (cols - 1)) * innerW);
    const y = pad.t + innerH - (n / max) * innerH;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.innerHTML = `<rect x="0" y="0" width="${w}" height="${h}" fill="#fbfcf9" />
    <polyline fill="none" stroke="${color}" stroke-width="1.8" points="${pts.join(" ")}" />
    ${axisTimes(series, w, pad, h - 5)}`;
}

function paintHttp(http) {
  document.getElementById("httpBlock").hidden = false;
  const hosts = Object.entries(http.by_host || {});
  const statuses = Object.entries(http.by_status || {});
  const max = Math.max(1, ...hosts.map((row) => row[1]));
  document.getElementById("httpHost").innerHTML = hosts.length
    ? hosts
        .map(
          ([host, n]) =>
            `<div class="tablero-bar"><span>${esc(hostShort(host))}</span><b style="--w:${(n / max) * 100}%"></b><i>${fmt(n)}</i></div>`
        )
        .join("")
    : "<p class='tablero-empty'>Todavía no hubo requests en este proceso.</p>";
  document.getElementById("httpStatus").innerHTML = statuses
    .map(([code, n]) => `<span class="tablero-chip ${code === "403" || code === "401" ? "is-hot" : ""}">${code} · ${fmt(n)}</span>`)
    .join("");
}

function paintStock(inv, tel) {
  document.getElementById("stockBlock").hidden = false;
  const sources = inv.sources || [];
  const max = Math.max(1, ...sources.map((row) => row.n));
  document.getElementById("stock").innerHTML = sources.length
    ? sources
        .map((row) => {
          const neu = ((tel.new || {}).by_source || {})[row.id] || 0;
          return `<div class="tablero-bar"><span>${esc(row.id)}</span><b style="--w:${(row.n / max) * 100}%"></b><i>${fmt(row.n)}${neu ? ` · +${fmt(neu)}` : ""}</i></div>`;
        })
        .join("")
    : "<p class='tablero-empty'>La base todavía no tiene avisos.</p>";
}

function paintCities(rows) {
  document.getElementById("cityBlock").hidden = false;
  const max = Math.max(1, ...rows.map((row) => row.n));
  document.getElementById("cities").innerHTML = rows.length
    ? rows
        .map(
          (row) =>
            `<div class="tablero-bar"><span>${esc(row.label || row.id)}</span><b style="--w:${(row.n / max) * 100}%"></b><i>${fmt(row.n)}</i></div>`
        )
        .join("")
    : "<p class='tablero-empty'>No hay lugares con avisos todavía.</p>";
}

function paintLogs(rows) {
  const block = document.getElementById("logBlock");
  const list = document.getElementById("logs");
  if (!rows.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  list.innerHTML = rows
    .slice()
    .reverse()
    .map((row) => `<li>${esc(row)}</li>`)
    .join("");
}

function fmtDur(sec) {
  const s = Math.max(0, Number(sec) || 0);
  if (s < 60) return `${Math.round(s)} s`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return r ? `${m} min ${r} s` : `${m} min`;
}

function paintVisits(rows) {
  const block = document.getElementById("visitBlock");
  const list = document.getElementById("visits");
  if (!block || !list) return;
  if (!rows.length) {
    block.hidden = true;
    return;
  }
  block.hidden = false;
  list.innerHTML = rows
    .map((row) => {
      const place = [row.city, row.country].filter(Boolean).join(", ") || "sin punto en el mapa";
      const pin = row.on_map ? "en el mapa" : "sin coordenadas";
      return `<li><b>${esc(row.when || "—")}</b> (${esc(fmtDur(row.sec))}) · ${esc(place)} · ${esc(row.source || "directo")} · ${pin}</li>`;
    })
    .join("");
}

function arm() {
  clearTimeout(timer);
  timer = setTimeout(tick, hurry ? 1500 : 4000);
}

function tick() {
  load().catch(() => {
    statusEl.hidden = false;
    statusEl.textContent = "No pude actualizar. Reintento…";
  });
  arm();
}

load()
  .catch(() => {
    statusEl.textContent = "No pude leer el tablero. Probá recargar.";
  })
  .finally(() => arm());
