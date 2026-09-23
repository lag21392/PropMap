# Informe de Publicidad y Crecimiento — PropMap
Fecha: 2026-09-22

## 1. Posicionamiento actual
PropMap es un visor de avisos inmobiliarios en venta en Argentina con mapa, score de ganga y filtros por ciudad/barrio/tipo. Valor diferencial: comparación de USD/m² por zona y visualización geográfica, sin ser portal.

ICP implícito:
- Compradores de vivienda en Argentina, foco CABA y AMBA, con interés en ganga y comparación de precios.
- Inversores inmobiliarios que buscan oportunidades por barrio.
- Usuarios técnicos que valoran datos y transparencia.

Problema: Sin marca pública, sin canales de adquisición definidos, sin contenido indexable. Tráfico depende de boca a boca y búsqueda directa.

## 2. Estado actual de publicidad y marketing
- **Acquisition**: No hay campañas pagas activas. No hay presupuesto asignado. Dependencia de SEO orgánico limitado por SPA.
- **Activation**: Onboarding inexistente. El usuario llega a index.html y debe descubrir filtros por sí mismo. No hay tutorial guiado, no hay primer valor claro en <30s.
- **Retention**: Cuenta opcional con favoritos cifrados. Sin emails, sin push, sin recordatorios de precios. Matomo presente pero sin activación de lifecycle.
- **Referral**: Sin programa de referidos, sin incentivos de compartir.
- **Revenue**: No hay monetización. Producto gratuito.

Stack de marketing actual:
- Matomo para analítica sin IP.
- SEO técnico básico: robots, sitemap, meta, JSON-LD.
- Sin CRM, sin email, sin ads manager.

## 3. Oportunidades de publicidad gratuita / de bajo costo

### Acquisition orgánica
1. **Programmatic SEO por ciudad**
   - Generar landings /ciudad/rosario, /ciudad/cordoba con texto único, mediana USD/m², barrios top.
   - Cada landing es un activo que puede rankear y ser compartido.
   - Costo: tiempo de desarrollo, sin media spend.

2. **Contenido de pulso del mercado**
   - Publicar informes mensuales “Pulso del mercado CABA” con gráficos exportables.
   - Distribuir en LinkedIn, X, Reddit r/ArgentinaInmobiliaria, grupos de Facebook de barrios.
   - Formato: imagen + datos + enlace a PropMap.

3. **Partnerships con creadores**
   - Ofrecer acceso a datos agregados a YouTubers de finanzas/inversión inmobiliaria a cambio de mención.
   - Sin costo de media, costo de relación.

### Activation
1. **Primer valor en 15 segundos**
   - Hero con búsqueda por ciudad prellenada y ejemplo de ganga visible sin scroll.
   - Tooltip de “cómo usar” al primer ingreso.

2. **Demo interactiva**
   - Flujo guiado que muestra un caso de ganga real en la ciudad del usuario.

### Retention
1. **Alertas de precio por ciudad**
   - Guardar ciudad y recibir aviso cuando aparecen nuevos avisos con score alto. Requiere email opt-in.

2. **Newsletter quincenal**
   - Resumen de oportunidades y cambios de precios por ciudad. Crecimiento orgánico de lista propia.

### Referral
1. **Compartir ganga**
   - Botón “Compartir aviso” con imagen generada del pin en mapa.
   - Incentivo: acceso a estadísticas premium al referir 3 amigos.

## 4. Publicidad paga viable cuando haya presupuesto

Prioridad baja hoy por falta de funnel y monetización. Cuando exista modelo de ingresos:
- **Google Search**: keywords “casas en venta [ciudad] mapa”, “departamentos baratos [barrio]”. CPC estimado medio-alto en inmobiliario.
- **Meta Ads**: audiencias de interés inmobiliario, lookalike de usuarios registrados.
- **YouTube**: pre-roll en canales de inversión.
- **Programática display**: retargeting de visitantes.

Recomendación: No invertir en paid hasta tener landings indexables, evento de conversión definido y LTV estimado.

## 5. Medición y experimentos
- Definir North Star: sesiones activas con filtro aplicado.
- Leading indicators: tiempo a primer filtro, % usuarios que guardan favorito, % retorno en 7 días.
- Experimentos de 2 semanas: hero con ciudad predefinida vs genérica; CTA “Ver gangas en CABA” vs “Explorar mapa”.

## 6. Roadmap 90 días
**Mes 1 - Fundación**
- Landings para top 10 ciudades con datos agregados.
- Sitemap dinámico y envío a Search Console/Bing.
- Hero con búsqueda rápida y ejemplo de ganga.

**Mes 2 - Contenido y distribución**
- Publicar 4 informes de pulso de mercado.
- Newsletter opt-in en panel de cuenta.
- Botón compartir aviso con imagen.

**Mes 3 - Retención**
- Alertas de nuevos avisos por ciudad.
- Onboarding tooltip y métricas de activación.

## 7. Riesgos
- Dependencia de scraping: cambios en fuentes pueden romper datos.
- Sin monetización: crecimiento sin retorno limita inversión en paid.
- Privacidad: no usar IP, mantener Matomo anonimizado.

## 8. Conclusión
PropMap tiene un activo valioso: datos comparativos geolocalizados. La publicidad más efectiva hoy es orgánica y basada en contenido: SEO programático por ciudad + informes de mercado + distribución en comunidades. Paid media solo tiene sentido tras consolidar funnel de activación/retención y definir unidad económica.

Próximos pasos recomendados:
1. Implementar landings por ciudad.
2. Definir evento de activación y medir con Matomo.
3. Lanzar newsletter de pulso de mercado.
