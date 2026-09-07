/**
 * The extension entry point — the module Shopify's bundler builds and the checkout sandbox runs.
 *
 * `register()` is `shopify.extend('WebPixel::Render', …)`, so importing this module has the side
 * effect of claiming the extension point. Nothing else lives here: everything decidable is in
 * `pixel.ts`, `beacon.ts`, `settings.ts` and `transport.ts`, which take their dependencies as
 * arguments and are therefore gradeable offline (C9). Code that can only run inside a real
 * Shopify sandbox is code no ticket verify can hold to anything, so there is exactly one line of
 * it and its whole content is the registration.
 *
 * The manifest that ships this file is `pixel/shopify.extension.toml`
 * (`type = "web_pixel_extension"`, `runtime_context = "strict"`).
 */
import { register } from '@shopify/web-pixels-extension'

import { installPixel } from './pixel.ts'

register((api) => {
  installPixel(api)
})
