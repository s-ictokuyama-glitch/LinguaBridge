import { defineConfig } from 'vitest/config';

export default defineConfig({
  // GitHub Pages (https://<user>.github.io/LinguaBridge/) 配信のためのベースパス
  base: '/LinguaBridge/',
  build: {
    rollupOptions: {
      input: {
        index: 'index.html',
        teacher: 'teacher.html',
        student: 'student.html',
      },
    },
  },
  test: {
    environment: 'node',
  },
});
