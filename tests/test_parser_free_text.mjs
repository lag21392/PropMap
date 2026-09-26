import { parseFreeText } from "../static/parse-free-text.js";

function test_basic() {
  const q = "casa 3 dormitorios con patio en Palermo hasta 200k USD";
  const out = parseFreeText(q);
  console.assert(out.type === "casa", "type");
  console.assert(out.minBeds === 3, "beds");
  console.assert(out.maxPrice === 200000, "price");
  console.assert(out.traits.includes("patio"), "trait");
  console.log("OK", out);
}

test_basic();
