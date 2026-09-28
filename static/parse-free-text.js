/* v1790354913 */
/**
 * parse-free-text.js
 * Parser ligero de búsqueda libre para PropMap.
 * Convierte texto natural en filtros existentes.
 * Estratégia: reglas con regex, sin dependencias externas.
 */

const TYPE_MAP = {
  casa: "casa",
  casas: "casa",
  departamento: "departamento",
  departamentos: "departamento",
  depto: "departamento",
  deptos: "departamento",
  ph: "ph",
  duplex: "ph",
  dúplex: "ph",
  terreno: "terreno",
  lote: "terreno",
  lotes: "terreno",
  local: "local",
  locales: "local",
  oficina: "oficina",
  oficinas: "oficina",
  galpon: "galpon",
  galpón: "galpon",
  galpones: "galpon",
};

const TRAIT_MAP = [
  { keys: ["credito","apto credito","hipotecable","hipoteca"], value: "credit" },
  { keys: ["dueno","dueño directo","dueño","owner"], value: "owner" },
  { keys: ["expensas bajas","bajas expensas","expensas baja"], value: "expenses" },
  { keys: ["urgente","venta urgente","rapido"], value: "urgent" },
  { keys: ["tranquilo","silencioso","quiet"], value: "quiet" },
  { keys: ["buen estado","bueno estado","renovado","impecable"], value: "good" },
  { keys: ["balcon","balcón"], value: "balcony" },
  { keys: ["luminoso","luz","luminoso"], value: "bright" },
  { keys: ["crecimiento","crece","en crecimiento"], value: "growing" },
  { keys: ["vista","vista abierta","vista panoramica"], value: "view" },
  { keys: ["patio","jardin"], value: "patio" },
  { keys: ["cochera","garage","garaje","parking"], value: "garage" },
  { keys: ["terraza","terraza"], value: "terrace" },
];

function normalize(str) {
  return String(str || "")
    .normalize("NFD").replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/-/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function parseNumber(str) {
  if (!str) return null;
  const cleaned = str.replace(/[^0-9.,kK]/g, "").replace(",", ".");
  if (/k$/i.test(str)) {
    const n = parseFloat(cleaned.replace(/k/i, ""));
    return isFinite(n) ? n * 1000 : null;
  }
  const n = parseFloat(cleaned);
  return isFinite(n) ? n : null;
}

function findTypes(text) {
  const found = [];
  const seen = new Set();
  if (/\bmono\s*h?\s*ambientes?\b/.test(text)) {
    found.push("departamento");
    seen.add("departamento");
  }
  for (const [k, v] of Object.entries(TYPE_MAP)) {
    if (seen.has(v)) continue;
    const re = new RegExp(`\\b${k}\\b`);
    if (re.test(text)) {
      seen.add(v);
      found.push(v);
    }
  }
  return found;
}

function findType(text) {
  if (/\bmono\s*h?\s*ambientes?\b/.test(text)) return "monoambiente";
  const found = findTypes(text);
  return found[0] || null;
}

function findTrait(text) {
  const found = new Set();
  for (const t of TRAIT_MAP) {
    for (const k of t.keys) {
      const re = new RegExp(`\\b${k}\\b`);
      if (re.test(text)) { found.add(t.value); break; }
    }
  }
  return [...found];
}

function parsePrice(text) {
  // hasta 200k USD, max 200000, 200 mil
  const re = /(?:hasta|max|menos de|up to)\s*([0-9]+(?:[.,][0-9]+)?\s*[kK]?)\s*(usd|ars|pesos)?/i;
  const m = text.match(re);
  if (!m) return null;
  let num = parseNumber(m[1]);
  if (!num) return null;
  // Detectar "200 mil"
  if (/mil/.test(text)) num = num * 1000;
  return { maxPrice: num, currency: m[2] ? m[2].toUpperCase() : null };
}

function parseM2(text) {
  const reMin = /(?:min|minimo|desde)\s*([0-9]+)\s*m2?/i;
  const reMax = /(?:max|maximo|hasta)\s*([0-9]+)\s*m2?/i;
  const reRange = /entre\s*([0-9]+)\s*y\s*([0-9]+)\s*m2?/i;
  const range = text.match(reRange);
  if (range) {
    return { minM2: parseInt(range[1]), maxM2: parseInt(range[2]) };
  }
  const min = text.match(reMin);
  const max = text.match(reMax);
  return {
    minM2: min ? parseInt(min[1]) : null,
    maxM2: max ? parseInt(max[1]) : null
  };
}

function parseRooms(text) {
  const re = /(\d+)\s*(dorm|dormitorios|habitaciones|amb|ambientes|baños?|bath)/gi;
  const beds = [];
  const rooms = [];
  const baths = [];
  let m;
  while ((m = re.exec(text)) !== null) {
    const num = parseInt(m[1]);
    const key = (m[2] || "").toLowerCase();
    if (/dorm|habit/.test(key)) beds.push(num);
    else if (/amb/.test(key)) rooms.push(num);
    else if (/baño|bath/.test(key)) baths.push(num);
  }
  return {
    minBeds: beds.length ? Math.max(...beds) : null,
    minRooms: rooms.length ? Math.max(...rooms) : null,
    minBaths: baths.length ? Math.max(...baths) : null
  };
}

function escapeName(token) {
  return token.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function pickBarrios(text, names) {
  const folded = normalize(text);
  const hits = [];
  const seen = new Set();
  for (const name of names || []) {
    const token = normalize(name);
    if (!token || token.length < 4 || seen.has(token)) continue;
    if (!new RegExp(`(?:^| )${escapeName(token)}(?: |$)`).test(folded)) continue;
    seen.add(token);
    hits.push(String(name));
  }
  hits.sort((a, b) => normalize(b).length - normalize(a).length);
  const kept = [];
  for (const name of hits) {
    const token = normalize(name);
    if (kept.some((longer) => normalize(longer) !== token && new RegExp(`(?:^| )${escapeName(token)}(?: |$)`).test(normalize(longer)))) continue;
    kept.push(name);
  }
  const drop = new Set();
  for (const left of kept) {
    for (const right of kept) {
      if (left === right) continue;
      if (new RegExp(`(?:^| )${escapeName(normalize(left))} de ${escapeName(normalize(right))}(?: |$)`).test(folded)) drop.add(left);
    }
  }
  const zoneWords = new Set(["norte", "sur", "este", "oeste", "centro"]);
  const result = kept.filter((name) => {
    if (drop.has(name)) return false;
    const token = normalize(name);
    return !(zoneWords.has(token) && new RegExp(`(?:^| )zona ${escapeName(token)}(?: |$)`).test(folded));
  });
  result.sort((a, b) => folded.indexOf(normalize(a)) - folded.indexOf(normalize(b)));
  return result;
}

function pickBarrio(text, names) {
  return pickBarrios(text, names)[0] || "";
}

function parsePlace(text) {
  const known = [];
  let barrioNames = [];
  try {
    const cities = window.lastCities || [];
    cities.forEach(c => {
      known.push({ id: c.id, label: normalize(c.label || c.id), type: "city" });
    });
    const placeBarrios = window.placeBarrios || {};
    const city = document.getElementById("cityFilter")?.value;
    if (city && placeBarrios[city]) barrioNames = placeBarrios[city];
  } catch {}
  const barrio = pickBarrio(text, barrioNames);
  if (barrio) return { place: barrio, type: "barrio" };
  const words = normalize(text).split(" ");
  for (let i = words.length - 1; i >= 0; i--) {
    const candidate = words.slice(i).join(" ");
    const hit = known.find(k => k.label === candidate || candidate.startsWith(k.label + " "));
    if (hit) return { place: hit.id, type: hit.type };
  }
  return null;
}

function parseFreeText(query) {
  const text = normalize(query);
  if (!text) return null;
  const out = {
    type: findType(text),
    city: null,
    barrio: null,
    zona: null,
    minBeds: null,
    minRooms: null,
    minBaths: null,
    minM2: null,
    maxM2: null,
    maxPrice: null,
    priceCurrency: null,
    traits: []
  };
  const price = parsePrice(text);
  if (price) { out.maxPrice = price.maxPrice; out.priceCurrency = price.currency; }
  const m2 = parseM2(text);
  if (m2) { out.minM2 = m2.minM2; out.maxM2 = m2.maxM2; }
  const rooms = parseRooms(text);
  if (rooms) {
    out.minBeds = rooms.minBeds;
    out.minRooms = rooms.minRooms;
    out.minBaths = rooms.minBaths;
  }
  out.traits = findTrait(text);
  const types = findTypes(text);
  const mono = /\bmono\s*h?\s*ambientes?\b/.test(text);
  if (types.length > 1) out.type = types;
  else if (types.length === 1) out.type = types[0];
  if (mono && types.length === 1 && types[0] === "departamento") {
    out.minRooms = 1;
    out.maxRooms = 1;
  }
  let barrioNames = [];
  let zonaNames = [];
  try {
    const city = document.getElementById("cityFilter")?.value;
    barrioNames = (window.placeBarrios && city && window.placeBarrios[city]) || [];
    zonaNames = [...document.querySelectorAll("#zonaFilter input")].map((box) => box.value);
  } catch (_) {}
  const barrios = pickBarrios(text, barrioNames);
  if (barrios.length === 1) out.barrio = barrios[0];
  else if (barrios.length > 1) out.barrio = barrios;
  const zonas = pickBarrios(text, zonaNames);
  if (zonas.length === 1) out.zona = zonas[0];
  else if (zonas.length > 1) out.zona = zonas;
  if (!barrios.length) {
    const place = parsePlace(text);
    if (place && place.type === "city") out.city = place.place;
  }
  return out;
}

// ES module export for tests
export { parseFreeText };

// Global for browser compatibility
if (typeof window !== "undefined") {
  window.parseFreeText = parseFreeText;
}

function applyParsedFilters(parsed, opts) {
  if (!parsed) return;
  const setVal = (id, val) => {
    const el = document.getElementById(id);
    if (!el || val == null || val === "") return;
    el.value = Array.isArray(val) ? val.join(",") : String(val);
  };
  const type = parsed.type || parsed.typeFilter;
  const city = parsed.city || parsed.cityFilter;
  const barrio = parsed.barrio || parsed.barrioFilter;
  const zona = parsed.zona || parsed.zonaFilter;
  const minBeds = parsed.minBeds != null ? parsed.minBeds : parsed.min_beds;
  const minRooms = parsed.minRooms != null ? parsed.minRooms : parsed.min_rooms;
  const minBaths = parsed.minBaths != null ? parsed.minBaths : parsed.min_baths;
  const minM2 = parsed.minM2 != null ? parsed.minM2 : parsed.min_m2;
  const maxM2 = parsed.maxM2 != null ? parsed.maxM2 : parsed.max_m2;
  const maxPrice = parsed.maxPrice != null ? parsed.maxPrice : parsed.max_price;
  if (type) setVal("typeFilter", type);
  if (!opts?.skipCity && city) setVal("cityFilter", city);
  if (barrio) setVal("barrioFilter", barrio);
  if (zona) setVal("zonaFilter", zona);
  if (minBeds != null) setVal("minBeds", minBeds);
  if (minRooms != null) setVal("minRooms", minRooms);
  if (minBaths != null) setVal("minBaths", minBaths);
  if (minM2 != null) setVal("minM2", minM2);
  if (maxM2 != null) setVal("maxM2", maxM2);
  if (maxPrice != null) setVal("maxPrice", maxPrice);
  const maxRooms = parsed.maxRooms != null ? parsed.maxRooms : parsed.max_rooms;
  if (maxRooms != null) setVal("maxRooms", maxRooms);
  if (parsed.dealBar != null && parsed.dealBar !== "") setVal("dealBar", parsed.dealBar);

  if (parsed.traits && parsed.traits.length) {
    document.querySelectorAll('input[name="listingTrait"]').forEach(cb => {
      cb.checked = parsed.traits.includes(cb.value);
    });
  }
  if (!opts?.skipRender && typeof render === "function") render();
}

// ES module export for tests
export { applyParsedFilters };

// Global for browser compatibility
if (typeof window !== "undefined") {
  window.applyParsedFilters = applyParsedFilters;
}
