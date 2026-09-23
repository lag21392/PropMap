# Ideas de monetización sin perder clientes — PropMap
Fecha: 2026-09-22

## Principio rector
Mantener el mapa y la búsqueda básicos 100% gratuitos y sin fricción. Monetizar valor agregado, no acceso. El cliente no debe sentir que paga por lo que antes era gratis.

## Modelo actual
Producto gratuito, sin registro obligatorio, sin paywall. Riesgo de perder usuarios si se introduce fricción.

## Opciones compatibles con retención

### 1. Afiliación con portales y corredores
Qué es: link de referencia a ficha original con parámetros UTM. Comisión por lead calificado o clic.
Cómo implementarlo: botón “Ver ficha original” ya existe. Añadir tracking de afiliados con portales que tengan programa.
Ventaja: no cambia UX, ingreso pasivo.
Riesgo: mantener transparencia y link abajo como ya se hace.

### 2. Alertas premium y watchlists
Gratis: favoritos locales.
Premium: alertas por email/Telegram cuando aparece aviso con score > X en ciudad/barrio, historial de precio, caída de precio.
Precio: tier bajo mensual/anual.
Retención: aumenta valor percibido, no bloquea mapa.

### 3. Informes de mercado bajo demanda
Gratis: pulso mensual público.
Pago: informe detallado por barrio/ciudad con mediana USD/m², evolución, top gangas, exportable.
Ideal para inversores y corredores.

### 4. API de datos agregados
No vender datos individuales de avisos, sino métricas agregadas anonimizadas: mediana por barrio, volumen, tiempo en mercado.
Licencia para desarrolladores, fintech, medios.
Cumple privacidad y no expone fuentes.

### 5. Patrocinio de contenido y branding
Sponsor de informes mensuales o de sección “Pulso del mercado”.
Sin banners invasivos en mapa, solo mención en informe y landing dedicada.

### 6. Donaciones / membresía de apoyo
Botón “Apoya PropMap” tipo Buy Me a Coffee. A cambio: badge, acceso temprano a features, newsletter exclusiva.
Mantiene producto libre.

### 7. White label para inmobiliarias
Ofrecer versión embebida del mapa con sus propios avisos filtrados.
Ingreso B2B recurrente, no afecta usuario final.

## Qué evitar
- Paywall del mapa o de búsqueda básica → fuga de usuarios.
- Venta de datos personales o IPs.
- Publicidad display intrusiva sobre el mapa.
- Cambiar descripciones o fotos de forma que parezca contenido propio sin atribución.

## Recomendación de ruta
Fase 1: Afiliación + donaciones. Cero fricción.
Fase 2: Alertas premium y watchlists.
Fase 3: Informes de mercado y API agregada.

Con esto se genera ingreso sin perder clientes, manteniendo PropMap como herramienta pública de referencia.

---

## Plan detallado por fase

### FASE 1 — Fundación (Semanas 1–4) — Cero fricción, ingreso pasivo

#### 1.1 Afiliación con portales y corredores
**Objetivo**: Monetizar clics salientes existentes sin cambiar UX.

**Tareas**:
- [ ] Inventario de portales con programa de afiliados (Zonaprop, Argenprop, Properati, MercadoLibre Inmuebles, corredores locales).
- [ ] Registrar PropMap en programas de afiliados (requiere tráfico mínimo; documentar requisitos).
- [ ] Añadir parámetros UTM a links “Ver ficha original”: `?utm_source=propmap&utm_medium=affiliate&utm_campaign=city_{ciudad}`.
- [ ] Implementar tracking de clics salientes en Matomo (evento `outlink_affiliate` con `portal`, `listing_id`).
- [ ] Dashboard interno: clics/día por portal, CTR por ciudad, ingresos estimados.

**Métricas de éxito**:
- CTR outlink ≥ 2% de sesiones con resultado.
- ≥ 3 portales activos con tracking.
- Ingreso estimado > $0 (validar modelo).

**Riesgos y mitigación**:
- Portales sin programa → mantener link directo sin UTM, no bloquear.
- Cambios en URLs de portales → revisión mensual automática (script de verificación).

**Esfuerzo**: 1–2 días dev + gestión administrativa.

---

#### 1.2 Donaciones / membresía de apoyo
**Objetivo**: Capturar disposición a pagar de power users sin fricción.

**Tareas**:
- [ ] Integrar Stripe Payment Links o Buy Me a Coffee (sin backend propio).
- [ ] Botón “Apoya PropMap” en header y panel de cuenta (solo logueados).
- [ ] Beneficios: badge “Supporter” en cuenta, acceso a canal Discord/Telegram privado, newsletter semanal “Gangas de la semana”.
- [ ] Página `/apoya` con transparencia: costos de servidor, scraping, tiempo.

**Métricas de éxito**:
- ≥ 10 donaciones/mes en mes 3.
- Ingreso recurrente mensual (MRR) donaciones > $50.

**Esfuerzo**: 0.5 día dev + configuración Stripe.

---

### FASE 2 — Activación y Retención (Semanas 5–12) — Valor recurrente

#### 2.1 Alertas premium y watchlists
**Objetivo**: Convertir usuarios recurrentes en suscriptores mensuales.

**Arquitectura**:
- Tabla `alerts` (user_id, city, barrio, property_type, max_price_usd, min_score, frequency, channel).
- Worker diario: consulta nuevos avisos, matchea alertas, envía email/Telegram.
- Canales: email (SendGrid/Postmark), Telegram Bot API.

**Tareas**:
- [ ] Extender `accounts.py`: modelo `Alert` + migración SQLite.
- [ ] UI en panel de cuenta: crear/editar alertas con preview de resultados actuales.
- [ ] Worker `alerts_worker.py` (cron diario 06:00 AR).
- [ ] Templates email: “Nuevas gangas en {barrio}” + link directo a mapa filtrado.
- [ ] Integración Telegram: bot `/start`, vincular cuenta, recibir alertas.
- [ ] Página de pricing: Free (favoritos locales) vs Pro (alertas ilimitadas, historial 90d, multi-ciudad).
- [ ] Stripe Subscription: $3/mes o $30/año (ARS equivalente).
- [ ] Webhook Stripe → activar/desactivar features Pro.

**Métricas de éxito**:
- Activación: ≥ 5% de usuarios registrados crean alerta en mes 1.
- Conversión Free→Pro: ≥ 3% a 90 días.
- Churn mensual Pro < 8%.
- MRR alertas > $200 en mes 3.

**Riesgos**:
- Spam/entregabilidad email → warmup IP, unsubscribe fácil, SPF/DKIM/DMARC.
- Costo envío → negociar plan transaccional; Telegram gratis.

**Esfuerzo**: 5–7 días dev.

---

#### 2.2 Informes de mercado bajo demanda (MVP)
**Objetivo**: Validar disposición a pagar por análisis.

**Tareas**:
- [ ] Script `generate_report.py`: input ciudad/barrio → salida PDF/HTML con mediana USD/m², evolución 12m, top 10 gangas, volumen, días en mercado.
- [ ] Landing `/informes/{ciudad}` con preview gratis (gráfico + 3 gangas) + CTA “Comprar informe completo $15”.
- [ ] Stripe Checkout one-off → entrega automática por email + descarga en cuenta.
- [ ] Versión gratis mensual “Pulso CABA” como lead magnet.

**Métricas**:
- Ventas/mes ≥ 5 en mes 2.
- Ingreso > $75/mes.

**Esfuerzo**: 3–4 días dev.

---

### FASE 3 — Escalabilidad B2B (Mes 4–6) — Ingreso recurrente alto

#### 3.1 API de datos agregados
**Objetivo**: Licenciar métricas anonimizadas a terceros.

**Producto**:
- Endpoints: `GET /api/v1/metrics/barrio?city=CABA&barrio=Palermo` → `{median_usd_m2, p25, p75, volume, avg_days_on_market, trend_3m}`.
- Rate limit: 100 req/día free tier, 10k/día paid.
- Auth: API key por cliente.

**Tareas**:
- [ ] Agregaciones SQL diarias materializadas en tabla `barrio_metrics_daily`.
- [ ] FastAPI router `/api/v1/metrics` con Pydantic response models.
- [ ] API key management: tabla `api_clients`, middleware auth.
- [ ] Documentación OpenAPI + portal developer (Scalar/Redoc).
- [ ] Planes: Developer $0 (100/día), Startup $99/mes (10k/día), Business $499/mes (100k/día).
- [ ] Outreach: fintechs hipotecarias, proptechs, medios, universidades.

**Métricas**:
- ≥ 3 clientes pagos en mes 6.
- MRR API > $500.

**Esfuerzo**: 7–10 días dev + ventas.

---

#### 3.2 White label para inmobiliarias
**Objetivo**: Ingreso B2B recurrente alto, bajo churn.

**Producto**: Iframe/embed del mapa filtrado a avisos de la inmobiliaria + branding suyo.

**Tareas**:
- [ ] Modo “embed” en `index.html`: parámetros `?embed=1&broker_id=xxx&hide_header=1`.
- [ ] Panel admin inmobiliaria: subir feed CSV/API, ver métricas de vistas/clics.
- [ ] Contrato SaaS: setup $500 + $199/mes.
- [ ] Piloto con 1–2 inmobiliarias amigas.

**Métricas**:
- ≥ 2 pilotos pagos en mes 6.
- MRR white label > $400.

**Esfuerzo**: 10–15 días dev + ventas.

---

## Roadmap consolidado 6 meses

| Mes | Foco principal | Entregables clave | Métrica norte |
|-----|----------------|-------------------|---------------|
| 1 | Afiliación + Donaciones | UTM tracking, 3 portales, botón donaciones | CTR outlink 2%, 10 donaciones |
| 2 | Alertas MVP | Worker, email, Telegram, Stripe Sub | 5% registrados con alerta |
| 3 | Alertas + Informes | Pricing, webhook, 1er informe venta | MRR $200+ |
| 4 | Informes + API | Agregaciones diarias, API keys, docs | 1 cliente API piloto |
| 5 | API + White label | Portal developer, 1 piloto white label | MRR API $200+ |
| 6 | Escalamiento | 3 clientes API, 2 white label | MRR total > $1,500 |

---

## Stack técnico necesario por fase

| Componente | Fase 1 | Fase 2 | Fase 3 |
|------------|--------|--------|--------|
| Pagos | Stripe Payment Links | Stripe Subscriptions + Webhooks | Stripe Subscriptions + Invoices |
| Email | — | SendGrid/Postmark transaccional | SendGrid marketing |
| Mensajería | — | Telegram Bot API | Telegram + WhatsApp Business API |
| Workers | — | Cron diario (systemd/cron) | Celery + Redis (si escala) |
| Analytics | Matomo events | Matomo + custom dashboards | Metabase/Superset |
| Auth | Sesión actual | Sesión + API keys | OAuth2 para partners |

---

## Presupuesto estimado (mensual, ARS/USD aprox.)

| Concepto | Fase 1 | Fase 2 | Fase 3 |
|----------|--------|--------|--------|
| Stripe fees | 2.9% + $0.30 | 2.9% + $0.30 | 2.9% + $0.30 |
| Email (SendGrid) | $0 | $15–30 | $50–100 |
| Servidor/DB | Actual | +$5–10 | +$20–50 |
| Dominio/SSL | Actual | Actual | Actual |
| **Total ops** | **~$5** | **~$30–50** | **~$100–200** |

*No incluye tiempo de desarrollo.*

---

## Decisiones abiertas (validar antes de invertir)

1. **Precio alertas Pro**: $3/mes vs $5/mes — test A/B en landing.
2. **Telegram vs Email**: medir apertura/click; Telegram puede tener mejor engagement en LatAm.
3. **Afiliación**: ¿portales aceptan afiliados sin factura? Verificar requisitos legales AR.
4. **White label**: ¿inmobiliarias tienen feed estructurado? Si no, costo de onboarding alto.
5. **API**: ¿demanda real o especulativa? Hacer 5 llamadas de discovery antes de construir.

---

## North Star y Leading Indicators

- **North Star MRR**: Ingreso recurrente mensual total (suscripciones + API + white label).
- **Leading**:
  - Usuarios con alerta activa (semanal).
  - Clics outlink afiliados (diario).
  - Leads B2B calificados (semanales).
  - Activación: % usuarios que crean alerta en día 1.

---

## Checklist de lanzamiento por hito

### Hito 1: Primer $ online (Semana 2)
- [ ] 3 portales con UTM tracking.
- [ ] Botón donaciones funcional.
- [ ] Matomo registra eventos `outlink_affiliate` y `donation_click`.

### Hito 2: Primer suscriptor Pro (Semana 6)
- [ ] Alertas worker corriendo en prod.
- [ ] Stripe webhook activando Pro.
- [ ] Email de bienvenida + primera alerta de prueba.

### Hito 3: Primer informe vendido (Semana 8)
- [ ] Landing `/informes/caba` con preview.
- [ ] Checkout one-off → entrega automática.

### Hito 4: Primer cliente API (Mes 4)
- [ ] API keys funcionando.
- [ ] Docs públicas.
- [ ] Contrato firmado.

### Hito 5: Primer white label (Mes 5)
- [ ] Embed mode probado.
- [ ] Panel admin básico.
- [ ] Facturación recurrente.

---

## Conclusión

La secuencia **Afiliación → Alertas → Informes → API → White label** permite:
- Validar demanda en cada paso antes de invertir en el siguiente.
- Mantener el core gratuito siempre.
- Diversificar ingresos: B2C (alertas, informes) + B2B (API, white label) + pasivo (afiliación, donaciones).
- Escalar MRR sin depender de un solo canal.

Próxima acción recomendada: **Semana 1 — registrarse en 3 programas de afiliados y poner botón de donaciones**. Es lo de menor riesgo y valida que hay disposición a pagar en la audiencia.
