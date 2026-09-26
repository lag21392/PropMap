import { toggleCompare, getCompareIds } from "../static/comparator.js";
global.localStorage = { store:{}, getItem(k){return this.store[k]||null}, setItem(k,v){this.store[k]=v} };
toggleCompare("a"); toggleCompare("b");
console.assert(getCompareIds().length===2,"compare");
console.log("compare OK");
