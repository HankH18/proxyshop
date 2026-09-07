/**
 * The extension manifest, held to the app that installs it.
 *
 * A web pixel has two halves and they fail independently. `shopify.extension.toml` is what
 * `shopify app deploy` builds and ships; `webPixelCreate` is what activates it on a shop, called
 * by `webPixelSettings()` in `apps/merchant/app/config/shopify.ts` and its Python mirror
 * `merchant_svc.install.config.web_pixel_settings`. Shopify validates the `settings` object that
 * mutation carries against the fields the manifest declares, so a key one side adds and the other
 * does not is a **failed install** — an error that shows up on a merchant's screen at onboarding
 * time, having passed every test on both sides.
 *
 * The merchant module is read as text rather than imported: the pixel workspace's `tsconfig.json`
 * has `rootDir: "."`, so pulling a file from `apps/merchant/` into this program would break
 * `tsc -b` for the whole workspace. Reading it fails loudly if the shape it looks for is gone.
 */
import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { COLLECTOR_URL_SETTING } from '../src/settings.ts'

const MANIFEST_URL = new URL('../shopify.extension.toml', import.meta.url)
const MERCHANT_CONFIG_URL = new URL(
  '../../apps/merchant/app/config/shopify.ts',
  import.meta.url,
)

const manifest = readFileSync(MANIFEST_URL, 'utf8')

/** The setting names the manifest declares, read off its `[extensions.settings.fields.X]` heads. */
function declaredSettingFields(toml: string): string[] {
  return [...toml.matchAll(/^\s*\[extensions\.settings\.fields\.([A-Za-z0-9_]+)\]\s*$/gm)]
    .map((match) => match[1] as string)
    .sort()
}

/**
 * The setting names `webPixelSettings()` puts in the object `webPixelCreate` is called with.
 *
 * Extracted from the returned object literal of that function. If the function or its literal
 * cannot be found this throws: a silent empty answer here would make the comparison below pass by
 * comparing nothing, which is the exact failure this file exists to prevent.
 */
function registeredSettingKeys(): string[] {
  const source = readFileSync(MERCHANT_CONFIG_URL, 'utf8')
  // Everything from the declaration to the first line that is a bare closing brace.
  const body = /export function webPixelSettings\([\s\S]*?\n\}/.exec(source)?.[0]
  if (body === undefined) {
    throw new Error(
      `could not find webPixelSettings() in ${MERCHANT_CONFIG_URL.pathname}; the pixel manifest ` +
        `has nothing to be checked against and this test would otherwise pass vacuously`,
    )
  }
  const returned = /return \{([\s\S]*?)\n\s*\}/.exec(body)?.[1]
  if (returned === undefined) {
    throw new Error(`webPixelSettings() no longer returns an object literal; adapt this reader`)
  }
  const keys = [...returned.matchAll(/^\s*([A-Za-z0-9_]+):/gm)].map((match) => match[1] as string)
  if (keys.length === 0) throw new Error('webPixelSettings() returned no recognisable keys')
  return keys.sort()
}

describe('the web pixel extension manifest', () => {
  it('declares itself a strict-context web pixel extension', () => {
    // `strict` has no DOM access and no access to the storefront's cookies or globals. C5 grants
    // this app no protected-customer-data scope, so running in the context that *cannot* read it
    // is the enforcement rather than the promise.
    expect(manifest).toMatch(/^type = "web_pixel_extension"$/m)
    expect(manifest).toMatch(/^runtime_context = "strict"$/m)
  })

  it('declares exactly the settings the merchant app registers the pixel with', () => {
    expect(declaredSettingFields(manifest)).toEqual(registeredSettingKeys())
  })

  it('declares the one setting the extension actually reads', () => {
    expect(declaredSettingFields(manifest)).toContain(COLLECTOR_URL_SETTING)
  })
})
