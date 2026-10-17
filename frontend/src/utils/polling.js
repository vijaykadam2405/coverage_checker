// Analysis reads only local saved evidence; it never calls the production Corpus.
export function analysisRefreshInterval(snapshot) {
  return snapshot?.active?.run_id === snapshot?.run?.id &&
    snapshot?.run?.id != null
    ? 15000
    : 0;
}
