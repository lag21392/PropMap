# Buenas prácticas de ramas Git: producción y desarrollo

## Objetivo
Mantener estabilidad en producción y permitir desarrollo continuo con dos ramas principales.

## Ramas principales
- `main` o `production`: código desplegado en producción. Solo recibe cambios liberados y verificados.
- `develop`: rama de integración para desarrollo. Acumula features y se prueba antes de liberar.

## Flujo recomendado
1. `develop` es la base para nuevas features.
2. Crear ramas de feature desde `develop`: `feature/nombre-corto`.
3. Al terminar, abrir PR a `develop`, revisar y mergear con squash o merge.
4. Cuando `develop` está estable, abrir PR a `main` para release.
5. Taggear la versión en `main`.

## Buenas prácticas
- Proteger `main` y `develop`: requerir PR, revisiones, CI verde, sin push directo.
- Nombres consistentes: `feature/`, `fix/`, `hotfix/`, `chore/`.
- Commits atómicos con mensajes descriptivos.
- CI/CD automático desde `main` a producción y desde `develop` a staging.
- Hotfix desde `main` a rama `hotfix/` y luego merge a ambas ramas.
- Mantener `develop` actualizada con `main` regularmente.
- Documentar releases y changelog.

## Reglas de merge
- `feature/*` → `develop`
- `develop` → `main` para release
- `hotfix/*` → `main` y `develop`
- Evitar merges de `main` a `feature` salvo para sincronizar.

## Consejos adicionales
- Usar branch protection y required status checks.
- Limpiar ramas cerradas.
- Usar conventional commits para automatizar versiones.
