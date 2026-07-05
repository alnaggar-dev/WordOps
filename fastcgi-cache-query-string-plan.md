# Plan: cache ad-tagged URLs in the FastCGI cache (query-string handling)

**Status:** DRAFT — design only, NOTHING implemented yet. Decisions still open (see
§7). This is a fork of WordOps, so changes go directly in the source templates and
are permanent (no upstream-update concern).

---

## 1. Goal

On a WooCommerce store driven by paid ads, every ad click arrives with a tracking
tag in the URL (`?gclid=…`, `?fbclid=…`, `?utm_source=…`). Today those requests are
**never served from cache**, so all ad traffic hits PHP/WordPress and is slow.

We want ad-tagged landing pages to be served from cache (fast), **without** losing
ad attribution and **without** caching things that must stay dynamic (cart, checkout,
add-to-cart, AJAX, search).

---

## 2. How the WordOps FastCGI cache decides things today

Two files drive it:

- `wo/cli/templates/fastcgi.mustache` → generated to `/etc/nginx/conf.d/fastcgi.conf`
- `wo/cli/templates/map-wp.mustache` → generated to `/etc/nginx/conf.d/map-wp.conf`

**The cache key** (`fastcgi.mustache:3`):

```nginx
fastcgi_cache_key "$scheme$request_method$host$request_uri";
```

`$request_uri` **includes the query string**, so `/p?gclid=A` and `/p?gclid=B` are
two *different* cache entries even though they render the same page.

**The skip decision** (`map-wp.mustache:72-96`). Five independent "no-cache" flags
are concatenated; the cache is used only when all five are `0`:

```nginx
map $is_args $is_args_no_cache {          # "?" present -> 1, none -> 0
    default 1;
    "" 0;
}
map $args $args_to_cache {                # query CONTAINS utm_ or fbclid -> 1
    default 0;
    "~*utm_" 1;
    "~*fbclid" 1;
}
map $is_args_no_cache$args_to_cache $query_no_cache {
    default 1;
    00 0;                                 # no query
    11 0;                                 # query AND (contains utm_/fbclid)
}
map $http_request_no_cache$http_auth_no_cache$cookie_no_cache$uri_no_cache$query_no_cache $skip_cache {
    default 1;
    00000 0;
}
```

`$skip_cache` then feeds both directives in the PHP location (`wpfc.mustache:14-15`):

```nginx
fastcgi_cache_bypass $skip_cache;   # 1 = don't read from cache
fastcgi_no_cache     $skip_cache;   # 1 = don't write to cache
```

So a query-string URL that isn't allowed shows `X-FastCGI-Cache: BYPASS` and is
neither served from nor written to cache.

Other independent exclusion layers (unchanged by this plan) — any one forces skip:

- **Cookie** (`map-wp.mustache:17-31`): logged-in users, `woocommerce_cart_hash`,
  post-password, etc.
- **URI path** (`map-wp.mustache:34-64`): `/cart/`, `/checkout/`, `/my-account/`,
  `/order-received/`, `/wc-api/`, `/wp-json/`, `/add_to_cart/`, `/wp-admin/`, …
- **Auth header**, **XHR header**.

---

## 3. Why the naive fix ("just cache any query string") is wrong

1. **It doesn't even help ad traffic.** Because the cache key contains the full
   query, and `gclid`/`fbclid` are **unique per click**, every ad click is a new key
   = a MISS = a one-hit-wonder copy nobody reuses. Ad pages stay slow.
2. **Cache bloat / cache-busting DoS.** `max_size=256M inactive=6h`
   (`fastcgi.mustache:2`). Thousands of unique-tag copies of the same page evict the
   pages you actually reuse. A bot appending `?x=1,2,3…` can deliberately flush the
   cache and hammer PHP-FPM. The current "skip all query strings" rule is what
   protects against this.
3. **It caches things that must stay dynamic.** `?add-to-cart=`, `?wc-ajax=`,
   `?wc-api=`, `?_wpnonce=` all travel in the query string and are only safe today
   *because* query strings are skipped.

---

## 4. Chosen approach — "tracking-only" collapse

**Rule:** cache a request only when its query string is *nothing but* known tracking
tags — and when it is, **file it under the clean URL** (tags dropped from the cache
key).

Consequences:

- `/p?gclid=A`, `/p?fbclid=B`, `/p?utm_source=fb`, and plain `/p` all share **one**
  cache entry. First ad visitor warms it; everyone after gets an instant HIT. ✅ goal.
- Any query with a *real* parameter (`add-to-cart`, `wc-ajax`, `wc-api`, `s=`,
  `_wpnonce`, filters, pagination) is **not** "only tracking tags", so it stays
  uncached — exactly like today. No separate blocklist needed; the rule excludes them
  automatically. It also **fixes an existing hole**: today `?add-to-cart=99&utm_source=x`
  is wrongly cacheable (substring match), this makes it a bypass again.
- **Key insight — "only tracking", not "contains tracking".** A mixed URL like
  `?utm_source=x&color=red` is NOT collapsed (otherwise we'd serve the unfiltered page
  to a filtered request). Mixed = treated as a normal, uncached query.
- **No cache wipe.** Normal (non-tracking) requests keep the exact same key they use
  today, so the existing cache stays valid. Only tracking URLs get the new key.
- **Existing safety layers untouched.** Logged-in/cart users still bypass via the
  cookie map; `/cart/`, `/checkout/`, etc. still bypass via the URI map. `$skip_cache`
  requires all five flags `0`, so any layer can still veto.

---

## 5. Exact changes

### 5a. `wo/cli/templates/map-wp.mustache` — replace lines 72-96

Replace the `$args_to_cache` / `$query_no_cache` block and add two cache-key maps:

```nginx
# $is_args is "?" when the request has a query string, "" otherwise
map $is_args $is_args_no_cache {
    default 1;
    "" 0;
}

# A request is "tracking only" when EVERY query param is a known ad/analytics
# tag and nothing else. Anchored ^...$ so ONE functional param (add-to-cart,
# wc-ajax, s=, filters, _wpnonce, ...) disqualifies the whole query.
map $args $tracking_only {
    default 0;
    "~*^((utm_[a-z_]+|gclid|gbraid|wbraid|gclsrc|dclid|gad_[a-z_]+|srsltid|fbclid|msclkid|ttclid)=[^&]*)(&(utm_[a-z_]+|gclid|gbraid|wbraid|gclsrc|dclid|gad_[a-z_]+|srsltid|fbclid|msclkid|ttclid)=[^&]*)*$" 1;
}

# Cacheable when: no query at all (00) OR query is tracking-only (11).
map $is_args_no_cache$tracking_only $query_no_cache {
    default 1;
    00 0;
    11 0;
}

# (unchanged) all five flags must be 0 to use the cache
map $http_request_no_cache$http_auth_no_cache$cookie_no_cache$uri_no_cache$query_no_cache $skip_cache {
    default 1;
    00000 0;
}

# --- cache-key normalization: collapse tracking tags to the clean URL ---
# Path portion of the ORIGINAL request (before "?"). We use $request_uri, NOT
# $uri, because after try_files rewrites to /index.php, $uri == "/index.php"
# for every page (which would collapse ALL pages into one entry -> disaster).
map $request_uri $request_path {
    "~^(?<reqpath>[^?]*)"  $reqpath;
    default                $request_uri;
}
# tracking-only -> key on clean path; everything else -> unchanged full key.
map $tracking_only $cache_key_uri {
    1        $request_path;
    default  $request_uri;
}
```

Single source of truth: the tracking-tag list appears once (`$tracking_only`) and
drives BOTH the skip decision (`$query_no_cache`) AND the cache key
(`$cache_key_uri`). They cannot drift apart.

### 5b. `wo/cli/templates/fastcgi.mustache` — line 3

```nginx
# before
fastcgi_cache_key "$scheme$request_method$host$request_uri";
# after
fastcgi_cache_key "$scheme$request_method$host$cache_key_uri";
```

### Regeneration

After editing templates, the generated nginx files must be refreshed and nginx
reloaded (e.g. `wo stack reload --nginx` / re-run the site update path, then
`nginx -t && systemctl reload nginx`). Confirm the exact regeneration command during
implementation.

---

## 6. Tracking-tag list (proposed default)

| Source | Params |
|---|---|
| Google Ads | `gclid`, `gbraid`, `wbraid`, `gclsrc`, `dclid`, `gad_*` (gad_source, gad_campaignid) |
| Google Shopping | `srsltid` |
| Facebook/Meta | `fbclid` |
| Bing (optional) | `msclkid` |
| TikTok (optional) | `ttclid` |
| Everyone | `utm_*` |

The list is trivial to extend later (it's a fork). **It must cover every tag your ad
platforms actually append** — any tag not listed makes the URL look "mixed" and it
won't cache.

---

## 7. Open decisions (must settle before building)

1. **Tracking plugin name(s).** Determines the conditional in §8. What captures the
   Facebook Pixel / Google Ads data? (e.g. "Facebook for WooCommerce", "PixelYourSite",
   "Pixel Manager for WooCommerce", a manual gtag snippet…)
2. **Tag list.** Accept the §6 default? Include Bing/TikTok? Anything custom?
3. **Functional queries stay uncached.** Search (`?s=`), filters (`?orderby=`,
   `?filter_*`), pagination (`?paged=`) remain uncached, same as today. OK, or do you
   want one of them (e.g. pagination) cached too? (Caching those is riskier — they
   change content and can bloat the cache.)
4. **All-or-nothing rule** — confirm this is the approach.

---

## 8. Conditional: server-side ad tracking

Caching a landing page means WordPress/PHP does **not** run for that request, so any
tracking that captures the ad tag **server-side on the landing page** would be
skipped for cached hits. Two branches, decided by the plugin name (§7.1):

- **Client-side capture** (the common case — pixel/gtag JS reads the tag in the
  browser and sets its own cookie; CAPI later reads that cookie): design ships as-is.
  Nothing lost.
- **Server-side capture on landing** (Meta CAPI / Google enhanced conversions /
  UTM-saved-to-order done in PHP at page load): leave *those specific tags* out of the
  `$tracking_only` list so they keep bypassing (page stays uncached, capture still
  fires). Lose caching only on those; everything else still benefits.

Do NOT assume "standard Google/Facebook pixel = client-side" without confirming the
actual plugin.

---

## 9. Verification checklist (before it goes live)

`nginx -t` + loading a URL twice only proves it parses and that a basic HIT works.
The real proof is exercising each behavior with a real request:

- [ ] `nginx -t` passes (catches syntax / unknown-variable errors).
- [ ] Plain URL `/p` and ad URL `/p?gclid=X` resolve to the **same** cache entry
      (same `X-FastCGI-Cache` HIT after one is warmed).
- [ ] Loading `/p?gclid=X` twice → second response is `HIT` (served from cache).
- [ ] Two different tags on the same page (`?gclid=X` vs `?fbclid=Y`) → second is a
      HIT of the first's entry (collapse confirmed).
- [ ] `?add-to-cart=…` → still BYPASS (not cached).
- [ ] `?wc-ajax=…` (cart fragments) → still BYPASS.
- [ ] `?wc-api=…` (payment callback form) → still BYPASS.
- [ ] `?s=search` → still BYPASS.
- [ ] Mixed `?utm_source=x&color=red` → still BYPASS (not collapsed).
- [ ] Logged-in / has-cart user on `/p?gclid=X` → still BYPASS (cookie layer intact).
- [ ] The tracking plugin still records the ad click end-to-end.

If any fails, do not ship — fix first.

---

## 10. Caveats / things to confirm during implementation

- **nginx map named-capture syntax** (`(?<reqpath>[^?]*)`): valid in nginx, but verify
  with `nginx -t`. Fallback: positional capture `~^([^?]*) $1;`.
- **Variable definition order across conf.d files.** `fastcgi.conf` (uses
  `$cache_key_uri`) may be parsed before `map-wp.conf` (defines it). nginx resolves map
  variables globally after full parse, and WordOps already does this cross-file with
  `$skip_cache`, so it should be fine — but `nginx -t` is the proof. Fallback if it
  errors: move the `fastcgi_cache_key` line into `map-wp.mustache` next to the maps.
- **Only affects the WPFC (FastCGI) stack.** Redis/`wpsc`/`wprocket`/`wpce` stacks use
  their own logic; this plan is scoped to `--wpfc` sites.
- **Purge:** `location ~ /purge` (`wpfc.mustache:18-19`) uses its own key
  `"$scheme$request_method$host$1"` — unaffected, but note cache purge keys off the
  clean path, consistent with the collapsed entries.
