/**
 * The merchant console's one-line mount for the demo nav bar.
 *
 * HOW TO TURN THE BAR ON. Add exactly this line to the top of
 * `apps/merchant/app/dashboard/main.tsx`, with the other imports:
 *
 *     import '../demo/mount'
 *
 * That is the whole change. This module mounts the bar as a side effect of being imported,
 * which is why it is one line rather than an import plus a call plus a JSX element — the
 * console's root component is owned by another lane and a demo aid should not need to be
 * threaded through it.
 *
 * WHY THIS FILE IS A COPY AND NOT AN IMPORT FROM THE BUYER APP. `nav.ts` and `demo-nav.css`
 * beside it are byte-for-byte the buyer's, and they have to be, because the two apps cannot
 * share a module through the build:
 *
 *   * `apps/merchant/tsconfig.json` sets `"rootDir": "."` with `"include": ["app/**"]`, and
 *     declares no `paths`; the buyer's copy is outside that program, and importing it is
 *     TS6059 under the `composite: true` project references the root tsconfig sets up.
 *   * The `vite build` stage of `apps/merchant/Dockerfile.web` COPYs `apps/merchant/`, the
 *     four workspace `package.json` files and the two tsconfigs — and nothing else. (Its
 *     build CONTEXT is the repo root, and its later COPY lines are wider, but those belong
 *     to the runtime Python stage and never reach the bundler.) So a file under
 *     `apps/buyer/` or a shared `apps/shared/` is simply not present when the bundle is
 *     built here.
 *   * The two apps are on different React majors on purpose (19 here-adjacent, 18 in this
 *     one, pinned by `apps/buyer/app/scaffold.test.ts`) and compile JSX two different ways.
 *
 * That is the same reason the eight woff2 files are duplicated between
 * `apps/buyer/app/journey/fonts/` and `apps/merchant/app/dashboard/fonts/` rather than
 * shared — a decision this repo already made, for these same constraints.
 *
 * A copy can drift, so it is not left on trust: `apps/buyer/app/demo/nav-parity.test.ts`
 * reads both files off disk and fails if the bytes stop matching. `nav.ts` imports nothing
 * and touches no framework, which is what makes a byte-identical copy possible at all.
 */
import { mountDemoNav } from './nav'
import './demo-nav.css'

mountDemoNav({ surface: 'merchant' })
