// Baked in at build time by the Dockerfile (VITE_SAIVA_VERSION). The running SPA
// compares this against the server's version to offer a "reload to update" nudge.
export const SPA_VERSION: string = import.meta.env.VITE_SAIVA_VERSION ?? "dev";
