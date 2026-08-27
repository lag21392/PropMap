---
name: lugares-via-api
description: >-
  Resuelve ciudades, localidades y barrios de Argentina solo por API (Georef y Nominatim),
  nunca con listas hardcodeadas. Usar al tocar geo, scrapers, filtros de ciudad, pines,
  post-análisis LLM o cualquier mención de un lugar.
---

# Lugares vía API

nunca debes usar valores de ciudades localidades barrios harcodeados en codigo,d eberias usar alguna api

## Regla

No agregar, ampliar ni restaurar catálogos de lugares en el código: ni `FOREIGN_PHRASES`, ni regex de Canning/Docta/Córdoba, ni `PROVINCE_SLUG` con las 24 provincias, ni barrios en `CITIES`, ni seeds de calles.

Si el aviso nombra un lugar, resolverlo con API y cachear el resultado.

## APIs

1. **Georef** (`https://apis.datos.gob.ar/georef/api`) — fuente principal: `provincias`, `localidades`, `municipios`, `asentamientos`.
2. **Nominatim** — solo si Georef no reconoce el nombre. Ya está en `app/places.py`.
3. **Overpass** — polígonos de barrios de la ciudad vista, cacheados. No inventar barrios.

Código: `app/place_api.py` (`lookup_place`, `listing_places`, `place_conflicts_city`).

## Qué sí puede estar en código

- Patrones genéricos (`en …`, `…, Provincia`, `barrio …`).
- Palabras de aviso que no son lugares (`venta`, `departamento`, `casa`, `terreno`).
- Coordenadas de trabajo que vengan de la API o de la base, no de un listado tuyo.

## Al filtrar un aviso

1. Extraer candidatos del título, dirección y barrio.
2. Resolver cada uno con `lookup_place` (cache primero).
3. Es de otro lado si la provincia o las coordenadas no calzan con la ciudad buscada.
4. Una calle con altura (`Neuquén al 800`) no es una ciudad.

## Tests

No pegas a la red. Usá `place_api.remember(nombre, {name, province, lat, lon, kind})`.
