# Análisis SEO PropMap — 2026-09-22

## Estado actual

**Puntos fuertes**
- Meta básicos correctos en `static/index.html` y `static/legal.html`: title, description, robots index,follow,max-image-preview:large, canonical con `__ORIGIN__`, Open Graph y Twitter Card.
- `app/seo.py` genera `/robots.txt` y `/sitemap.xml`. `SecureHeaders` aplica `X-Robots-Tag: noindex` a `/api`, `/stats`, `/tablero`, `/flujo`, `/admin`, `/verificar`, `/q/`.
- JSON-LD WebSite + WebApplication en home, WebPage + BreadcrumbList en legal.
- Cabeceras de seguridad: HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy, CSP, Cache-Control immutable para /static/.
- Páginas legales con Ley 25.326 y redirecciones 301 de /privacidad, /terminos, /aviso → /legal.

**Limitaciones críticas**
- Contenido indexable casi nulo. La app es SPA: todo el catálogo se carga vía `/api/listings`. El sitemap solo contiene `/` y `/legal`.
- Sin URLs públicas por ciudad/barrio/tipo. Los filtros viven en estado JS, no en rutas rastreables.
- Título y H1 genéricos, sin jerarquía por ciudad.
- Sin `og:image`. Sitemap estático sin `lastmod` dinámico.
- E-A-T bajo: no hay páginas de autoridad por ciudad con texto único, datos de mercado estáticos y enlaces internos.

## Mejoras gratuitas prioritarias

### Técnico / Crawl
1. **Sitemap dinámico por ciudad**
   - Generar `/sitemap-cities.xml` con URLs tipo `/ciudad/{slug}` y `/ciudad/{slug}/{tipo}`.
   - Actualizar `lastmod` con última actualización de la base.
   - Enviar a Google Search Console y Bing Webmaster.
2. **Robots y canonical**
   - Mantener `Disallow: /api/`, `/stats/`, `/tablero/`, `/flujo/`, `/admin/`.
   - Canonical auto-referencial en cada landing, noindex en vistas con query params.
3. **Rendimiento**
   - Preload ya usado para CSS/JS. Asegurar `loading="lazy"` en imágenes de avisos.
   - Compresión Brotli en Nginx/Traefik si está disponible.
4. **Seguridad y privacidad**
   - Mantener Matomo sin IP. Usar `X-Robots-Tag` ya implementado.

### On-page / Contenido
1. **Landings por ciudad**
   - Plantilla mínima HTML con H1 `Casas y departamentos en venta en Rosario`, texto único 200-400 palabras, mediana USD/m², barrios top, enlace al mapa con parámetros.
   - Estructura: H1 ciudad → H2 barrios → H2 cómo se calcula el score.
2. **Structured data**
   - `RealEstateListing` o `ItemList` para avisos renderizados en HTML.
   - `BreadcrumbList` en landings.
   - `WebSite` con `SearchAction` para búsqueda interna.
3. **Meta y OG**
   - Añadir `og:image` con logo o captura del mapa.
   - Títulos 50-60 chars con keyword al inicio: `Casas en venta en CABA · Mapa con score de ganga | PropMap`.
   - Descripciones únicas por ciudad, 150-160 chars.
4. **Enlaces internos**
   - Menú de ciudades, enlaces cruzados barrio→ciudad→tipo.
   - Sitemap HTML visible en footer.

### Medición gratuita
- Google Search Console: cobertura, rendimiento, enlaces.
- Bing Webmaster Tools.
- Lighthouse en DevTools.
- PageSpeed Insights.
- Screaming Frog SEO Spider free ≤500 URLs.
- SEO Minion extensión Chrome.

## APIs gratuitas útiles

**No requieren API key o tier gratuito amplio**
- **Google Search Console API**: acceso a datos de rendimiento. Requiere OAuth, no API key.
- **Bing Webmaster API**: similar, OAuth.
- **OpenStreetMap Nominatim**: geocodificación, uso gratuito con límites y User-Agent. Ya se usa.
- **Schema.org validator**: sin key.

**Con tier gratuito limitado**
- **Google Maps Geocoding / Places**: útil si se quiere enriquecer direcciones. Necesita API key de Google Cloud con facturación habilitada y cuota gratuita mensual. Alternativa gratuita: Nominatim.
- **OpenAI / Gemini API**: para enriquecimiento LLM. Gemini tiene tier gratuito, requiere API key.
- **Cloudflare Workers KV / R2**: para cache estático, tier gratuito.
- **Upptime / Uptime Kuma**: monitoreo gratuito self-hosted.

**Privacidad-first**
- **DuckDuckGo Instant Answers**: no requiere key.
- **W3C Markup Validator**: sin key.

## Checklist rápido
- [ ] Generar landings `/ciudad/{slug}` con texto único.
- [ ] Sitemap dinámico con ciudades y tipos.
- [ ] `og:image` en home y legal.
- [ ] JSON-LD `ItemList` en landings.
- [ ] Envío a Search Console y Bing.
- [ ] Auditoría Lighthouse SEO + Performance.
- [ ] Mapa de enlaces internos ciudades↔barrios.

## Notas de privacidad
El usuario no quiere uso de IP. Mantener Matomo con anonimización, no integrar Google Analytics. Usar Nominatim con User-Agent propio y respetar políticas de uso.
