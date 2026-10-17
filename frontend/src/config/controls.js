// The Control Room selects output types, not the internal discovery algorithm.
export const CAPTURE_OPTIONS = [
  {
    label: "HTML pages",
    types: ["HTML"],
    help: "Collect pages and compare their text with Corpus.",
  },
  {
    label: "PDFs",
    types: ["PDF"],
    help: "Collect PDF URLs only. No document content is downloaded.",
  },
  {
    label: "Documents",
    types: ["DOCUMENT"],
    help: "Collect document URLs only, including Word and spreadsheets.",
  },
  { label: "Images", types: ["IMAGE"], help: "Collect image URLs only." },
  {
    label: "Other links",
    types: ["API", "FEED", "MEDIA", "OTHER"],
    help: "Collect feeds, media, API endpoints and other resource URLs.",
  },
];
