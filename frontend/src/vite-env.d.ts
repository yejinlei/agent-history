/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_OFFLINE?: string;
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
