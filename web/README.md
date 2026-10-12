# Photon Commander frontend

React and TypeScript browser interface, with the sky map isolated in its own iframe.
See the root README for setup, assets and licences.

```sh
npm ci
npm run typecheck
npm run build
npm run test
```

`npm run api:codegen` regenerates the committed API contract from the local
application definitions without connecting to hardware. `npm run dev` uses
`SSC_DEV_PROXY_PORT` (default 7777) for an explicitly started local backend.
