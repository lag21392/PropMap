# Feature: Features y Monetización Fase 1 — PropMap

**Fecha:** 2026-09-25
**Origen:** analysis/features.md §2.1, §2.3, §3.1, §3.3 | analysis/monetizacion.md §FASE 1
**Estado:** en progreso

## Objetivo
Implementar las features de alta prioridad y bajo esfuerzo de la Fase 1 de ambos análisis: modo oscuro, búsqueda por texto libre, filtros guardados, responsive móvil, afiliación UTM y botón de donaciones.

## Alcance Fase 1

### 1. Modo oscuro (features.md §2.1)
- CSS variables con tema claro/oscuro
- Toggle manual persistido en localStorage
- Respetar `prefers-color-scheme`
- Ajustar paleta: `--bg`, `--panel`, `--paper`, `--ink`, `--muted`
- Tiles oscuros para Leaflet

### 2. Búsqueda por texto libre (features.md §2.2 - MVP simple)
- Input de texto libre arriba de filtros
- Parsing con regex + mapeo a filtros existentes
- Ejemplo: "casa 3 dormitorios Palermo hasta 200k USD"

### 3. Filtros guardados (features.md §2.3)
- Guardar combinaciones de filtros en localStorage
- Cargar filtros guardados al iniciar

### 4. Responsive móvil básico (features.md §3.3)
- Breakpoints para layout 4 columnas
- Panel de filtros colapsable
- Mapa redimensionado para touch

### 5. Afiliación UTM (monetizacion.md §1.1)
- Añadir parámetros UTM a links "Ver ficha original"
- Tracking de clics salientes en Matomo
- Dashboard interno de CTR

### 6. Botón de donaciones (monetizacion.md §1.2)
- Integrar Stripe Payment Links o Buy Me a Coffee
- Botón "Apoya PropMap" en header
- Página /apoya con transparencia

## Tareas
1. [x] Modo oscuro: crear design tokens CSS con tema claro/oscuro
2. [x] Modo oscuro: implementar toggle + prefers-color-scheme
3. [x] Modo oscuro: aplicar tiles oscuros al mapa Leaflet
4. [ ] Búsqueda por texto libre: input + parser regex
5. [ ] Filtros guardados: persistir en localStorage
6. [ ] Responsive móvil: breakpoints y panel colapsable
7. [ ] Afiliación: añadir UTM tracking a outlinks
8. [ ] Afiliación: evento Matomo `outlink_affiliate`
9. [ ] Donaciones: botón + página /apoya + Stripe integration

## Criterios de aceptación
- Modo oscuro funcional con toggle y preferencia del sistema
- Búsqueda por texto libre parsea ejemplos clave
- Filtros se guardan y cargan correctamente
- Layout responsive en móviles
- Links afiliados tienen UTM tracking
- Botón de donaciones redirige a Stripe
- Tests existentes pasan

## Métricas de éxito
- Adopción modo oscuro >40%
- Uso búsqueda por texto >30%
- CTR outlink afiliados ≥2%
- ≥10 donaciones/mes en mes 3