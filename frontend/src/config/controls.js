export const SOURCES = {
  SITEMAP: ["Sitemaps", "Recursively inspect published sitemap indexes."],
  STATIC: [
    "Page links",
    "Follow navigation, iframe, button URLs and resource references.",
  ],
  RENDERED: ["Rendered pages", "Inspect public content in a normal browser."],
  API: [
    "Observed API / XHR",
    "Follow URLs actually exposed in published responses.",
  ],
  STRUCTURED_DATA: [
    "Structured data",
    "Read published JSON-LD and hydration links.",
  ],
  HTTP_LINK: [
    "HTTP link headers",
    "Follow published next/canonical relationships.",
  ],
  FEED: ["Feed links", "Follow RSS/Atom entry URLs."],
  FILTER_GET: [
    "GET filters",
    "Use values exposed by read-only website filters.",
  ],
  FILTER_BROWSER: [
    "Browser filters",
    "Explore supported read-only filter controls.",
  ],
  PAGINATION: [
    "Pagination",
    "Follow observed links and probe numeric boundaries.",
  ],
  TABS: [
    "Lazy content tabs",
    "Open empty same-page tabs; loaded sections stay one page.",
  ],
  LOAD_MORE: [
    "Load More / scrolling",
    "Explore more results until no new content appears or you stop the scan.",
  ],
};
export const NUMERIC_CONTROLS = {
  browser: [
    [
      "browser_challenge_wait_seconds",
      "Public verification wait (seconds)",
      0,
      30,
      1,
      "Wait for ordinary browser verification; never solve a CAPTCHA automatically.",
    ],
  ],
  performance: [
    [
      "website_delay_seconds",
      "Website request spacing (seconds)",
      1,
      30,
      0.05,
      "Minimum per-host delay; robots and Retry-After may increase it.",
    ],
    [
      "request_timeout_seconds",
      "Website timeout (seconds)",
      5,
      120,
      1,
      "Wall-clock request budget.",
    ],
    [
      "max_retries",
      "Website retries",
      0,
      4,
      1,
      "Bounded retry attempts; failed content remains disclosed.",
    ],
  ],
  corpus: [
    [
      "corpus_interval_seconds",
      "Corpus spacing (seconds)",
      2,
      30,
      0.5,
      "Minimum spacing, with adaptive slowdown and Retry-After.",
    ],
    [
      "corpus_timeout_seconds",
      "Corpus timeout (seconds)",
      60,
      180,
      1,
      "Allows slow HDD reads without changing website timeouts.",
    ],
  ],
};
