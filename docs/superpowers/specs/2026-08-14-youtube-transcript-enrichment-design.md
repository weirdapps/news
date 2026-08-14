# YouTube Transcript Enrichment

**Date:** 2026-08-14
**Status:** Approved (pending user spec review)
**Profile:** `stack` only
**Context:** The `stack` and `digest` profiles carry 8 unique YouTube channel
Atom feeds, but a YouTube Atom entry has no `content` — feedparser exposes only
`media:description`, which is the marketing blurb the uploader wrote to attract
viewers. A live sample of the Fireship feed returned 480 characters of which a
third was sponsor copy and UTM parameters. That blurb is what the synthesis
model currently reads, and it is what the owner objected to: "totally skewed and
far from the truth."

## The two stacked bottlenecks

Fixing this needs both halves. Either alone is useless.

1. **We only have the description.** No transcript is fetched anywhere.
2. **Only a prefix reaches the model.** `stack_synth.py:123` sends
   `article.content[:300]`; `synthesizer.py:218` sends 200. Even with a full
   transcript in hand, everything past the cap is discarded.

Bottleneck 2 also sets a trap: 300 characters of a *transcript* is worse than
300 characters of a *description*, because a description leads with its thesis
while a transcript opens cold. The sampled Fireship transcript begins "On
Monday, the company that spent the last year starving Llama...", which tells a
curation model nothing. **A naive fetch-transcript-and-overwrite-content change
would make the digest worse.** The distillation step below exists to avoid this.

## Goal

For every new video on a `stack` channel, extract what was actually said,
distil it into a compact factual abstract with sponsor copy and hype removed,
and get that abstract in front of both the relevance scorer and the synthesis
model — without adding latency to the synthesis run or destabilising dedup.

## Non-goals

- Video-native understanding. Claude accepts text, images and PDFs, not video,
  so the `claude` CLI path cannot ingest one. Gemini can take a YouTube URL
  natively but that is a different provider and contradicts the standing "all
  LLM calls via the `claude` CLI" rule. Deferred, not designed around.
- Frame extraction for on-screen-only content (code in an editor that is never
  read aloud). Real gap for Fireship and ThePrimeagen, but it requires
  downloading video and costs more than the rest of this feature combined.
- Enrichment for `digest`, `monitor`, or `market`. (Decision 9 removes a channel
  from `digest`, which is a source-list deletion, not enrichment. No `digest`
  code path changes.)
- Repairing `manage-youtube`'s `transcript.ts` in `plessas-lab` (see Decision 2).
- Lifting the 300-char truncation for non-video articles.
- Wiring up `extract_content()` (`news/processor.py:199`), which is dead code
  with zero callers despite CLAUDE.md documenting a trafilatura full-text stage.
  Pre-existing, noted, out of scope.

## Measured constraints

These were established empirically on 2026-08-14 and drive the decisions below.

| Finding | Evidence |
|---|---|
| Transcripts work from the Mac | `youtube-transcript-api` returned 1,137 words for video `G55HSGpuh1M` vs a ~70-word description, a 16x depth increase |
| Transcripts are blocked from the VPS | Same call from Hetzner raises `RequestBlocked`: "an IP belonging to a cloud provider". The watch page served the VPS 1.2 MB with **zero** `captionTracks`; the Mac got 1 |
| The existing tool is broken everywhere | `manage-youtube`'s `transcript.ts` fails on the Mac too: `Player API failed: 400`. Its `youtube-caption-extractor` dependency has rotted |
| Bloomberg TV dominates volume | 1,922 of 2,015 stored YouTube rows (95%), all auto-generated clip blurbs |
| The stack pool overruns its cap | 425 / 888 / 381 / 218 articles fetched on recent days against `max_digest_articles: 150` |
| A column can be added cheaply | `_migrate_db()` (`news/storage.py:34-46`) already applies an idempotent `ALTER TABLE` list; SQLite `ADD COLUMN` is metadata-only, so the 604 MB file is not rewritten |

## Architectural decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Transcript fetching runs on the **Mac**, not the VPS | Owner decision. YouTube blocks the VPS by IP. Alternative was a residential proxy on the VPS; rejected in favour of zero recurring cost using the IP already proven to work. |
| 2 | Use Python `youtube-transcript-api`, not the existing `transcript.ts` | The TypeScript tool is broken against YouTube's current player API. Python also keeps the harvester in the same language and test harness as the rest of the repo. |
| 3 | Distil each transcript to an abstract **once per video, at harvest time** | Moves the expensive work off the synthesis critical path entirely. A video harvested at 10:00 is already distilled when the 13:00 stack run reads it, and the result is cached forever. Zero added synthesis latency. |
| 4 | The abstract lives in its **own field and column**, never in `content` | `compute_hash()` is `title + content[:200]`. Overwriting content would re-insert under a new hash any video first stored before its abstract existed (Mac asleep, or a video published minutes before a run). That is a permanent double-store on every video that outruns the harvester, not a one-time backfill wave. |
| 5 | Store the abstract in `news.db`, not attach it post-load | The digest pool comes from `get_articles_since()` reading the DB, so an in-memory-only abstract vanishes for anything fetched on a previous run. Persisting it also lets the relevance scorer see it (Decision 6). |
| 6 | Relevance scoring reads the abstract | The pool runs 2-6x over the 150 cap, so selection genuinely drops content. A video whose description never says "Claude Code" but whose transcript does now earns the +30 `claude_mention` bonus it currently misses. Without this, the depth we pay for can be filtered out before anyone reads it. |
| 7 | Abstracts get an 800-char synthesis allowance; other articles keep 300 | Owner decision. ~20 video items in a 150-article run is about +2.5k tokens, noise against the 150s synthesis timeout, and it is the difference between a headline-ish lede and actual extracted facts. |
| 8 | One SQLite file per writer; never two writers on one file | `sync-news-db-from-vps.sh` already rsyncs `news.db` **VPS → Mac** every 30 min to feed the local MCP server, making the Mac copy a read-only replica. `transcripts.db` therefore flows the other way, Mac → VPS. The existing sync script is untouched. |
| 9 | Drop `YouTube: Bloomberg TV` from `digest` | Owner decision. 95% of YouTube volume, auto-generated clip blurbs, and `digest` already carries Reuters Business, FT, FT International, WSJ Markets, Bloomberg via Google and CNBC as text feeds covering the same beat with real prose. |
| 10 | Channel list is read from `config/stack/sources.yaml`, not duplicated | Adding a channel stays a one-place edit. The harvester derives its work queue from the same config the pipeline fetches. |

## Components

### A. Harvester — `scripts/youtube_harvest.py` (Mac, launchd, hourly)

1. Parse `config/stack/sources.yaml`, select `rss_feeds` entries whose URL matches
   `youtube.com/feeds/videos.xml`, extract `channel_id`.
2. Fetch each channel's Atom feed; collect `yt_videoid`, title, `published`.
3. Diff against `transcripts.db`; keep only video IDs not already terminal.
4. For each: fetch transcript via `youtube-transcript-api`.
5. Distil via one `subprocess.run(["claude", "--model", "sonnet", "--print"], ...)`
   call. Prompt instructs: extract concrete claims, findings and technical
   substance; drop sponsor reads, subscribe requests and hype framing; no
   preamble; target 600-800 characters.
6. Upsert into `transcripts.db`.
7. `rsync` the store to the VPS. Folded into the harvester rather than a separate
   job, so there is one artifact and one schedule.

### B. Store — `data/transcripts.db` (Mac-authored, gitignored)

```sql
CREATE TABLE IF NOT EXISTS transcripts (
    video_id        TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    title           TEXT NOT NULL,
    published_at    TEXT,
    transcript      TEXT,           -- raw; retained so abstracts can be regenerated
    abstract        TEXT,           -- what the pipeline consumes
    status          TEXT NOT NULL,  -- ok | no_captions | fetch_failed | summary_failed
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    updated_at      TEXT NOT NULL
);
```

Sizing: ~6 KB transcript + ~1 KB abstract per video, so 1,000 videos is roughly
7 MB. Retaining the raw transcript costs little and allows re-distillation under
an improved prompt without re-fetching from YouTube.

### C. Enrichment — `news/transcripts.py` (VPS, read-only)

- `extract_video_id(url) -> str | None` — matches `youtube.com/watch?v=<id>`
  and `youtu.be/<id>`; returns `None` for anything else.
- `load_abstracts(db_path, video_ids) -> dict[str, str]` — single query over the
  batch's IDs, `status = 'ok'` only. Returns `{}` if the file is absent.
- `enrich_articles(articles, db_path) -> tuple[int, int]` — sets
  `transcript_abstract` in place; returns `(enriched, total_video_items)` for
  the coverage report.

Hook point: in `run_stack_pipeline`, immediately after `fetch_all_sources()` and
**before** the `for article in raw_articles:` loop that sets `pipeline` and calls
`compute_hash()` (`main.py:1141-1146`), so the abstract is present in time for
`process_articles` to score it and for `insert_article` to persist it. Placing it
before that loop is safe precisely because Decision 4 keeps the abstract out of
`content`, leaving the hash input untouched.

### D. Schema and consumer changes

| File | Change |
|---|---|
| `news/models.py` | `Article.transcript_abstract: str = ""` |
| `news/storage.py` | Append `ALTER TABLE articles ADD COLUMN transcript_abstract TEXT` to `_migrate_db()`; add the column to `init_db`, to the `insert_article` INSERT, and to the row → `Article` mapper |
| `news/processor.py` | `compute_relevance_score` match text becomes title + content + abstract |
| `news/stack_synth.py` | Snippet becomes `transcript_abstract[:800]` when present, else `content[:300]` |
| `news/deliver.py` + stack template | Coverage line in the footer |
| `config/sources.yaml` | Remove the `YouTube: Bloomberg TV` entry |

## Data flow

```text
MAC (residential IP, launchd hourly)          VPS (news-stack.timer, 13:00 Athens)
──────────────────────────────────────        ──────────────────────────────────────
config/stack/sources.yaml
  └─ YouTube channel IDs
       ↓
  Atom feeds → new video IDs
       ↓
  youtube-transcript-api  (~1,100 words)
       ↓
  claude --model sonnet --print
  "extract facts, strip sponsor copy"
       ↓
  abstract (~600-800 chars)
       ↓
  transcripts.db ──── rsync push ──────────→  read-only lookup by video ID
                                                       ↓
                                              fetch → ENRICH → process → store → synthesize
                                                                  ↑            ↑
                                                            scoring sees   800-char
                                                             abstract       snippet
```

## Failure handling

Every failure path degrades to current behaviour. That property is what makes
this safe to ship incrementally.

| Failure | Behaviour |
|---|---|
| Mac asleep / harvester did not run | Store is stale; recent videos have no abstract; pipeline emits description-only items exactly as today |
| `transcripts.db` absent on the VPS | `load_abstracts` returns `{}`; pipeline proceeds unchanged |
| Video has no captions | `status = no_captions`, terminal, never retried |
| Transcript fetch fails transiently | `status = fetch_failed`, `attempts += 1`, retried on subsequent harvester runs until `attempts` reaches 3, then left alone |
| Distillation call fails | `status = summary_failed`; transcript retained so only the cheap half is retried |
| rsync fails | Harvester logs and exits non-zero; store remains valid locally and pushes on the next run |

**Observability.** The owner accepted the Mac-asleep risk on the condition it not
be silent. The stack run logs `N/M YouTube items enriched` and carries the same
line in the email footer, so a stale store is visible in the artifact actually
read rather than only in a VPS log nobody opens.

## Testing

All tests mock `youtube-transcript-api` and the `claude` subprocess. No network,
no LLM calls, consistent with the existing suite.

- `extract_video_id` across `watch?v=`, `youtu.be/`, and non-YouTube URLs
- Store upsert and each status transition, including that `no_captions` is not retried
- Harvester skips video IDs already terminal in the store
- Harvester derives its channel list from `config/stack/sources.yaml`
- `enrich_articles` attaches abstracts, leaves non-YouTube articles untouched,
  and returns accurate coverage counts
- `load_abstracts` returns `{}` when the database file does not exist
- `compute_relevance_score` awards `claude_mention` on an abstract-only match
- `stack_synth` uses 800 chars for an abstract and 300 for a plain article
- `insert_article` round-trips `transcript_abstract` through the DB
- Migration is idempotent on an already-migrated database

## Open items

1. **Channel roster.** The coverage half of the original request. The owner has
   yet to nominate channels. Current `stack` roster is Fireship, Matt Wolfe,
   AI Explained, Two Minute Papers, IndyDevDan, ThePrimeagen, NetworkChuck.
   This is config content and blocks nothing structural; new channels are a
   one-line YAML addition each once the harvester exists.
2. **Harvester cadence.** Hourly is proposed. The only real constraint is that
   it should run at least once between a video appearing in an Atom feed and the
   13:00 stack run.
