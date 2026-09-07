/**
 * Reading the settings the merchant app registered this pixel with.
 *
 * `webPixelCreate` is called with `WebPixelInput.settings`, a free-form JSON scalar, built by
 * `webPixelSettings()` in `apps/merchant/app/config/shopify.ts` and mirrored by
 * `merchant_svc.install.config.web_pixel_settings`. Exactly three keys: `collectorUrl`,
 * `shopDomain`, `apiVersion`. Only the first one changes what this extension does, and
 * `shopify.extension.toml` declares all three, because Shopify validates the settings a
 * `webPixelCreate` sends against the fields the extension manifest declares.
 *
 * **Nothing here throws.** This code runs inside a shopper's checkout page. A pixel that raises
 * on a misconfigured setting is a pixel that reports a fault into a sandbox nobody is reading,
 * having already failed to do the one job it had; the honest degradation is to install, observe
 * nothing, and leave the authoritative `orders/paid` webhook as the sole record of the purchase
 * — which R4 already says it is.
 */

/** The setting that decides where a beacon goes. */
export const COLLECTOR_URL_SETTING = 'collectorUrl'

/**
 * The collector endpoint this pixel should POST to, or `null` when it was not configured with a
 * usable one.
 *
 * A usable one is an absolute `http:` or `https:` URL. `http:` is accepted because the offline
 * substrate (C9) serves the merchant app on loopback and a pixel that refused it could not be
 * exercised without a network; in production the storefront is `https:` and the browser's
 * mixed-content rules, not this function, are what forbid the plain-text case. A relative path
 * is refused outright: relative to the checkout page it would resolve against the *shop's*
 * origin, so it would silently beacon a shopper's join keys at Shopify rather than at this app.
 */
export function readCollectorUrl(settings: unknown): string | null {
  if (typeof settings !== 'object' || settings === null) return null
  const raw = (settings as Record<string, unknown>)[COLLECTOR_URL_SETTING]
  if (typeof raw !== 'string') return null
  const candidate = raw.trim()
  if (candidate === '') return null
  let parsed: URL
  try {
    parsed = new URL(candidate)
  } catch {
    return null
  }
  return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? candidate : null
}
