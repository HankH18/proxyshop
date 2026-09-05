/**
 * `vite` resolves a CSS import to a stylesheet side effect. `tsc -b` type-checks the `app`
 * tree and knows nothing about that, so the wildcard module is declared here.
 * Ambient only — this file deliberately has no top-level import or export.
 */
declare module '*.css' {
  const stylesheet: string
  export default stylesheet
}
