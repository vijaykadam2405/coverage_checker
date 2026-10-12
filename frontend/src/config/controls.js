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
    "Explore more results until content stabilizes or a safeguard is reached.",
  ],
};
export const NUMERIC_CONTROLS = {
  scope: [
    [
      "max_urls",
      "Inventory safeguard",
      100,
      200000,
      100,
      "No depth cutoff. Reaching this limit records incomplete exploration.",
    ],
  ],
  browser: [
    [
      "max_browser_pages",
      "Rendered-page safeguard",
      1,
      2000,
      1,
      "Maximum pages inspected in the browser.",
    ],
    [
      "max_browser_actions",
      "Actions per page",
      1,
      200,
      1,
      "Bounded filter, tab and Load More interactions.",
    ],
    [
      "browser_challenge_wait_seconds",
      "Public verification wait (seconds)",
      0,
      30,
      1,
      "Wait for ordinary browser verification; never solve a CAPTCHA automatically.",
    ],
  ],
  pagination: [
    [
      "max_filter_values",
      "Values per filter",
      1,
      500,
      1,
      "Site-provided values only; combinations are not blindly generated.",
    ],
    [
      "maximum_pagination_probe",
      "Numeric probe safeguard",
      2,
      1048576,
      1,
      "A safeguard, not an asserted website maximum.",
    ],
  ],
  performance: [
    [
      "website_delay_seconds",
      "Website request spacing (seconds)",
      1.5,
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
      3,
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
