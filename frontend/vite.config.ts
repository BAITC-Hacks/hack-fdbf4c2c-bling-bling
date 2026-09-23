import { defineConfig } from 'vite';
import { resolve } from 'node:path';
export default defineConfig({root: resolve(process.cwd(), 'frontend'), build: {outDir: 'dist'}, server: {proxy: {'/api': 'http://127.0.0.1:8080'}}});
