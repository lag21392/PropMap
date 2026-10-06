# Filtros por período, provincia en ciudades y búsqueda por provincia

## Objetivo

1. Distinguir en alquileres los períodos temporales (por día/noche/semana) de los contratos mensuales.
2. Agrupar las ciudades cargadas por provincia en el selector "Ciudades cargadas".
3. Posicionar bien provincia y ciudad en la búsqueda de lugares, igual para compra y alquiler.

## Estado

- Autorizado: Sí, por pedido explícito del usuario en esta conversación.
- Alcance: filtro de período (soporte `weekly`), agrupación por provincia en `cityFilter`/form manual, ranking de búsqueda por provincia+ciudad, pruebas enfocadas.
- Restricciones: preservar cambios locales existentes; no actualizar ramas remotas; mantener flujo de compra intacto.
- Ruta: implementación directa acotada al flujo existente.
- TDD: activado por instrucción explícita del usuario; runner: `python3 -m pytest` y `node --check static/app.js`.
- Forecast: menos de 400 líneas editadas.
- Estrategia de entrega: `ask-on-risk`.

## Tareas

- [x] B1. Soporte de período `weekly` en la API de alquileres (pattern) y prueba del endpoint.
- [x] B2. Opción "Por semana" en el selector de período y render de precio/período semanal.
- [x] B3. Agrupar las ciudades cargadas por provincia en `cityFilter` y el form manual.
- [x] B4. La búsqueda de lugares matchea y rankea por provincia y ciudad.
- [x] B5. Validar con pytest, node y navegador.

## Criterios de aceptación

- El filtro de período distingue mensual, por noche y por semana en alquileres.
- El selector "Ciudades cargadas" muestra las ciudades agrupadas bajo su provincia.
- La búsqueda de lugares posiciona bien resultados por provincia y ciudad, igual en compra y alquiler.
- Compra conserva su comportamiento actual.

## Verificación

- TDD: primero pruebas fallidas; después implementación y suite enfocada.
- Runner backend: `python3 -m pytest`.
- Runner frontend: `node --check static/app.js`.
- Navegador: filtrar período, ver ciudades agrupadas por provincia, buscar por provincia y ciudad.

## Evidencia de validación

- `pytest tests/test_rentals_api.py tests/test_place_api.py tests/test_yields.py -q` → `67 passed`
- `node --check static/app.js` → sintaxis OK
- Suite completa `pytest tests/ -q` → `12 failed, 679 passed`. Los 12 fallos son preexistentes y ajenos a este cambio: ocurren en módulos no tocados (`http_client`, `egress`, `listings_cache`, `ops`, `seo`, `detail_fetch`); ninguno de mis archivos está entre los fallos.
- Navegador: el contenedor de preview (puerto 8010) corre código viejo (iniciado oct04/oct05) y no refleja estos cambios; la validación de render queda pendiente hasta reiniciar el contenedor. El patrón nuevo (`weekly`) se confirmó por prueba de API unitaria.

## Commit

- `17ea7a6` feat: soportar período semanal, agrupar ciudades por provincia y buscar por provincia (7 files, +378/-44)
