# AGENTS Guide for `src/mash/api/web-admin`

The admin dashboard SPA. Built with Vite into `../static/admin/` and served at
`/admin` by `mash.api.admin_ui`.

## What Must Stay True
- The SPA reads data only through `src/lib/api.js` against `/api/v1`; it adds no
  new transport and relies on the `mash_api_key` cookie for auth.
- Routes are declared in `src/App.jsx`; the nav tab list lives in
  `src/components/Shell.jsx`. The two stay in sync.
- The build output directory is `../static/admin/`; `mount_admin_ui` depends on
  `index.html` being present there.
- `/admin` mounting stays best-effort: a deployment without the built bundle
  must keep working with the route simply absent.

## Change Rules
- New data needs come from existing or new `/api/v1` routes in
  `src/mash/api/routes/`, surfaced through an `api.*` method in `lib/api.js` —
  do not fetch ad hoc inside route components.
- Adding a tab means a route in `App.jsx`, a component in `src/routes/`, and a
  nav entry in `Shell.jsx`; update `README.md`'s tab table.
- Keep presentation primitives in `src/components/` and reuse `State` +
  `useApi` for load/empty/error handling.

## Minimal Validation
- `npm run build` succeeds and emits into `../static/admin/`.
- The changed tab loads against a running host (`/admin`) without console errors.
