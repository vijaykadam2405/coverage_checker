# Durable scan checkpoints

## How to use

1. Click **Pause scan** and wait for saving/stopping to finish.
2. Keep the same saved website inventory selected.
3. Click **Resume scan**. Restarting the application does not discard that inventory.

Checkpoints are automatic; there is no separate save button or new tuning option.
The scan retains its original discovery/capture settings and continues with serial,
paced Corpus checks. Use Resume rather than Start website scan, which creates a new
inventory intentionally.

Example: URLs 1–49 are complete and URL 50 finishes website discovery, then the user
pauses before browser exploration. Resume starts URL 50's browser stage, then its
Corpus checks, then the remaining queued URLs. It does not fetch URLs 1–49 again
or redo URL 50's completed static discovery.

## Storage and execution

`crawler_coverage_auditor/services/checkpoints.py` contains the durable progress
store and its read-only API projection. `AuditDatabase` creates/migrates the tables
additively without deleting existing data. All updates use the same SQLite writer
connection as the audit; no parallel worker or Corpus write is introduced.

| Table | Saved information |
| --- | --- |
| scan_checkpoints | Current URL, next/current stage, last completed URL, sitemap setup completion, pause timestamp and reason |
| url_checkpoints | Completed website discovery, completed browser exploration and fully processed URL flags |
| sitemap_checkpoints | Pending/completed sitemap files, parent, body hash and next entry index |
| pagination_probe_checkpoints | Reusable public HTML/terminal 404 probe responses and original capture timestamps; no cookie/credential headers or file bodies |

The normal URL queue and existing HTML snapshots/Corpus results remain the source
of truth for collected content. Checkpoints supplement them rather than replacing
them. Completing a URL updates its checkpoint and queue state in the same commit.
Completing another saved check cannot erase the interrupted URL's cursor.

On resume, an interrupted FETCHING/RENDERING URL becomes queued again, with its
finished stage flags preserved. The scheduler prioritizes this saved current URL
ahead of the normal depth-ordered queue. Corpus-only interruptions reuse saved live
HTML and successful presence results, then retry only the incomplete verification.

Sitemap discovery retains its pending queue and completed files. An unfinished file
is fetched again, then processing continues at its saved entry index. The body hash
must match; a changed file restarts its entry traversal, merging existing URLs and
capturing newly inserted entries instead of skipping them. Completed index files
do not need to be traversed again just to reconstruct the pending child queue.

Pagination probing retains completed response evidence. Resume can rebuild an
unfinished exponential/binary-search calculation using saved responses rather than
requesting them again. A previously verified maximum is reused for the same audit,
including after a pause while adding its remaining page range. The saved numeric
maximum still requires the original boundary proof; a block is never treated as a
final page. This does not introduce a maximum URL/page count.

Intentional pause is not a Corpus outage. Its checkpoint is saved before generating
reports; pause history becomes historical after all pending work is exhausted.
Actual website/Corpus access errors and uncertainty remain recorded.

## What cannot be guaranteed

- An unfinished browser tab/filter/scroll interaction cannot be reconstructed as
  an identical live browser process. That current page's browser step may reopen;
  finished page stages and URLs are not restarted.
- A crash between an external response and its durable commit can require a retry
  of that unfinished read. Exactly-once network execution is not promised.
- Robots permission/session validity may be refreshed after application restart.
  These safety reads are not a restart of collected website pages.
- Checkpoints describe one inventory snapshot. They are not proof that the live
  website or production Corpus has not changed since the previous checks.
- Old inventories lack some stage markers. Saved completion/results are preserved;
  their one unrecorded in-flight stage may need a safe replay. Explicit recovery
  from historical count cutoffs is separate from normal checkpoint resume.

## API and testing

`GET /api/runs/<id>/checkpoint` returns bounded checkpoint metadata only. The run
summary includes the same object. No saved probe HTML, cookies, secret values or
Corpus bodies are returned. Older unmigrated databases return `available: false`
without being changed by a dashboard read. Resume remains an explicit local POST.

`tests/test_stage_checkpoints.py` covers pauses after each completed stage, an
unfinished browser step, database reopening after a crash, preserving cursor
priority, text-only retry, sitemap cursors and changed files, safe probe caching,
original capture dates, read-only API projection, and mid-pagination restart
without repeating completed probe fetches. Existing serial ordering, duplicate,
coverage, performance and historical-inventory tests are retained.

## Applying the update

An already running backend must be restarted to load this implementation. Pause and
wait for saving first, stop its terminal with Ctrl+C, run
`npm.cmd --prefix frontend start` from the project directory, refresh the dashboard,
then resume the saved inventory. The existing React Pause/Resume actions are reused;
no frontend rebuild is required for this backend-only update.
