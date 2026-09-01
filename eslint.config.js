import js from '@eslint/js'
import tseslint from 'typescript-eslint'
export default [
  { ignores: ['**/node_modules/**','**/dist/**','**/build/**','**/.next/**',
              '**/.react-router/**','.venv/**','.swarm-loop/**','.pkgroot/**'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
]
