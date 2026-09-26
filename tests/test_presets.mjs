import { loadPresets, savePresets, currentFilters } from "../static/filter-presets.js";
global.localStorage = { store:{}, getItem(k){return this.store[k]||null}, setItem(k,v){this.store[k]=v} };
savePresets([{name:"t",state:{}}]);
const p = loadPresets();
console.assert(p.length===1,"presets");
console.log("presets OK");
