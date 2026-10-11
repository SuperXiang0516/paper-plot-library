# v1 implementation contract

Project: `paper-plot-library`; Python package: `paper_plot_library`; site name: 论文绘图案例库.
This is independent of aging-sequencing-daily. Never execute author code.

## Transport (owned by main)

`HttpClient(cache_dir, max_requests=150, timeout=25, retries=3)` exposes:
`get_json(url, params=None, headers=None, ttl=86400, max_bytes=6000000)` -> decoded object;
`get_text(...)` -> str;
`get_bytes(...)` -> bytes;
`post_json(url, payload, headers=None, max_bytes=1000000)` -> decoded object.
Raises `FetchError` with sanitized host/status, and `.status` integer or None.
Only HTTPS approved provider hosts. No author code execution, credentials in public files, or arbitrary remote URL fetch.

## Adapter (sources.py / repository.py / figures.py)

`discover(client, settings)` -> list of candidate dicts. `lookup_paper(client, doi)` -> paper dict.
Paper uses seed-compatible fields: id,paper,doi,journal,date,year,paperURL,pmcid,abstract,publicationTypes.
`inspect_repository(client, record, max_files=6)` -> dict:
status,url,commit,license,scripts (list),inputs (list),figureMappings (list),limitations (list).
Each script: path,url,sha256,language,inputs,outputs,evidence (bounded textual line references).
Never treat a plotting function / filename alone as verified article-figure correspondence.
`inspect_article(client, record, asset_dir, include_supplements=True)` -> dict:
status,license,version,xmlURL,figures (list),supplements (list),codeURLs (list),limitations (list).
Figure: id,label,caption,sourceURL,asset (site-relative `assets/...`),sha256,width,height,license,attribution,reuseStatus.
Article XML and licensed images come from current PMC OA cloud, final publisher version preferred.
Do not auto-publish third-party or ND/NC/unknown-license images; links only. Validate DOI.
Only images associated with actual JATS figure graphic references, not supplements/GUI logos.
Supplement attachments capped (count and per-file bytes), licensed files private `.ppl/downloads`; publish manifest/links only.
No fake inferred crop. Optional safe approved-box crop function with checks.

## Enrichment (enrichment.py)

`enrich_metrics(client, record, settings)` -> dict: status,if,jcr,cas,source,retrieved_at,metric_year (unknown if absent).
`summarize(client, record, evidence, settings)` -> dict: status,title_zh,summary_zh,learning_points,limitations,evidence_ids.
API failure is per-record recoverable. No key -> missing_key. AI may not invent input fields or license facts.
Settings attributes: llm_api_url,llm_api_key,llm_model,easyscholar_key,ncbi_api_key,ncbi_email,openalex_key,github_token,include_supplements,discovery_limit,max_new_per_day,max_ai_calls.
AI calls bounded by main; provider HTTP retries also count as calls (client tracks `.post_attempts`).

## Public frontend (web/, site.py)

Build normal static site (no Codex host bridge). `build_site(root, catalog, reports)` copies UI from web into site; writes site/data/catalog.json and site/data/updates.json. Root `site/` is rebuildable preview output; unrelated user-added files are preserved and excluded from whitelist publication export. `data/catalog.json` is persistent source.
Catalog top-level schemaVersion:4, papers (seed-compatible), inputSpecs (by id), dimensions (forms/functions registry), generatedAt, authorCodeExecuted:false.
Per-paper automation: status (seed/pending/ready/partial/error), stages, lastCheckedAt, errors; repository; article; metrics; ai.
Use article.figures assets only if reuseStatus=allowed; never show a paper webpage as an image.
Compact AND filters graphForms + analysisFunctions; independent many-to-many selections. Tabs inputs/explanation/source/notes. Escaped text / safe URLs, no HTML injection.
Clearly distinguish static inspection from execution, provisional tags from verified tags, and candidates from publishable cases.

## Main integration

Main owns config.py,http.py,storage.py,pipeline.py,seed importer,CLI, workflows,docs.
Daily cap 5 distinct new cases persisted in data/ledger.json; repeat runs idempotent. Up to 5 existing cases repaired per run separately, not called new publications.
Fixtures/offline demo explicitly labelled synthetic; never published as real papers.
