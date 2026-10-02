import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The backend's CORS allow-list is port-specific. Without strictPort,
    // Vite silently falls through to 5174 when 5173 is already taken and the
    // app dies with an opaque "Network error" instead of a clear message.
    strictPort: true,
  },
})
