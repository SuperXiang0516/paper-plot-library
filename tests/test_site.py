import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from paper_plot_library.site import build_site, public_catalog


PROJECT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "/Users/xiangzhoufu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"


def sample():
    return {
        "schemaVersion": 4, "authorCodeExecuted": False,
        "generatedAt": "2026-10-11T12:00:00Z", "papers": [{
            "id": "paper-1", "paper": "<img src=x onerror=alert(1)>", "doi": "10.1000/test",
            "graphForms": ["scatter", "heatmap"], "formClassificationStatus": "approved",
            "analysisFunctions": ["association"], "functionClassificationStatus": "approved",
            "runPerformed": False, "paperURL": "https://example.org/paper", "api_key": "must-not-publish",
            "abstract": "Private fetched full abstract is not a public response dump.",
            "article": {"status": "ok", "license": "CC BY 4.0", "figures": [{
                "id": "fig-1", "label": "Figure 1", "asset": "assets/paper-1/fig-1.png",
                "reuseStatus": "allowed", "sourceURL": "https://example.org/image.png", "width": 2000, "height": 1200,
            }], "raw_response": "must-not-publish"},
            "repository": {"status": "partial", "scripts": [{"path": "plot.py", "url": "https://github.com/author/repo/blob/123/plot.py", "raw_code": "must-not-publish", "evidence": ["L2: input-file read"]}]},
            "metrics": {"status": "ok", "if": "5.2", "jcr": "Q1", "source": "https://example.org/metrics?secret=must-not-publish"},
            "ai": {"status": "ok", "summary_zh": "中文解读", "review_required": True, "prompt": "must-not-publish"},
        }],
        "inputSpecs": {"paper-1": {"tree": "data/input.csv", "files": [{"name": "input.csv", "fields": [["age", "number", "actual header", "age in years"]]}]}, "unrelated": {"tree": "must-not-publish"}},
        "dimensions": {"forms": {"all": [{"id": "scatter", "label": "散点图"}, {"id": "heatmap", "label": "热图"}]}, "functions": {"all": [{"id": "association", "label": "相关分析"}]}},
    }


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(PROJECT / "web", self.root / "web")
        image = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture-image-header")

    def tearDown(self):
        self.temporary.cleanup()

    def test_builds_static_site_and_whitelisted_data(self):
        catalog = sample()
        output = build_site(self.root, catalog, [{"status": "ok", "newCount": 1, "settings": {"token": "must-not-publish"}}])
        serialized = (output / "data" / "catalog.json").read_text()
        result = json.loads(serialized)
        self.assertNotIn("must-not-publish", serialized)
        self.assertNotIn("abstract", result["papers"][0])
        self.assertNotIn("unrelated", result["inputSpecs"])
        self.assertNotIn("raw_code", result["papers"][0]["repository"]["scripts"][0])
        self.assertTrue(result["papers"][0]["ai"]["review_required"])
        self.assertTrue((output / "assets" / "paper-1" / "fig-1.png").is_file())
        self.assertEqual(catalog, sample(), "Build must not mutate persistent source catalogue.")
        self.assertNotIn("must-not-publish", (output / "data" / "updates.json").read_text())
        self.assertEqual((output / "app.js").read_bytes(), (self.root / "web" / "app.js").read_bytes())

    def test_license_change_removes_previously_owned_image_not_user_files(self):
        catalog = sample()
        output = build_site(self.root, catalog, [])
        user_file = output / "assets" / "user-note.txt"
        user_file.write_text("leave untouched")
        catalog["papers"][0]["article"]["figures"][0]["reuseStatus"] = "link_only"
        build_site(self.root, catalog, [])
        self.assertFalse((output / "assets" / "paper-1" / "fig-1.png").exists())
        self.assertEqual(user_file.read_text(), "leave untouched")
        result = json.loads((output / "data" / "catalog.json").read_text())
        self.assertNotIn("asset", result["papers"][0]["article"]["figures"][0])

    def test_missing_image_cannot_be_displayed_as_available(self):
        catalog = sample()
        catalog["papers"][0]["article"]["figures"][0]["asset"] = "assets/absent.png"
        output = build_site(self.root, catalog, [])
        figure = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["article"]["figures"][0]
        self.assertEqual(figure["reuseStatus"], "asset_unavailable")
        self.assertNotIn("asset", figure)

    def test_preserves_selected_inputs_automatic_candidates_and_stale_ai_separately(self):
        catalog = sample()
        selected = copy.deepcopy(catalog["inputSpecs"]["paper-1"]["files"])
        candidates = {"tree": "unrelated/input.csv", "status": "全仓库候选", "files": [{"name": "unrelated/input.csv", "fields": [["cohort", "未知", "实际表头", "待核"]], "observedFieldReferences": [{"name": "age", "line": 12, "kind": "code_field_reference"}]}]}
        catalog["inputSpecs"]["paper-1"]["automaticInputs"] = candidates
        catalog["papers"][0]["ai"].update(status="stale", stale_reason="来源已更新", last_attempt_status="missing_key", generated_at="2026-10-10T12:00:00Z")
        output = build_site(self.root, catalog, [])
        public = json.loads((output / "data" / "catalog.json").read_text())
        self.assertEqual(public["inputSpecs"]["paper-1"]["files"], selected)
        self.assertEqual(public["inputSpecs"]["paper-1"]["automaticInputs"], candidates)
        self.assertEqual(public["papers"][0]["ai"]["status"], "stale")
        self.assertEqual(public["papers"][0]["ai"]["stale_reason"], "来源已更新")

    def test_failure_provenance_and_metric_retention_survive_public_projection(self):
        catalog = sample()
        catalog["papers"][0]["article"]["licenseURL"] = "https://creativecommons.org/licenses/by/4.0/"
        catalog["papers"][0]["repository"].update(lastError="GitHub 403", errorStatus=403, last_attempt_status="unavailable", retained_from_previous=True)
        catalog["papers"][0]["metrics"].update(retained_field_asof={"if": "2025-01-01T00:00:00Z"}, persistent_cache_hit=True, last_attempt_status="error")
        projected = public_catalog(catalog)["papers"][0]
        self.assertEqual(projected["article"]["licenseURL"], "https://creativecommons.org/licenses/by/4.0/")
        self.assertEqual(projected["repository"]["errorStatus"], 403)
        self.assertTrue(projected["repository"]["retained_from_previous"])
        self.assertEqual(projected["metrics"]["retained_field_asof"]["if"], "2025-01-01T00:00:00Z")
        self.assertTrue(projected["metrics"]["persistent_cache_hit"])

    def test_build_downgrades_ready_when_published_reviewed_asset_missing(self):
        catalog = sample()
        paper = catalog["papers"][0]
        paper["automation"] = {"status": "ready", "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected", "ai": "generated"}}
        paper["repository"]["status"] = "inspected"
        paper["repository"]["commit"] = "a" * 40
        paper["repository"]["scripts"][0].update(sha256="b" * 64, url="https://github.com/author/repo/blob/" + "a" * 40 + "/plot.py")
        paper["repository"]["inputs"] = [{"path": "input.csv", "headerSHA256": "c" * 64, "fields": [{"name": "age"}], "scriptURL": paper["repository"]["scripts"][0]["url"]}]
        paper["ai"]["status"] = "generated"
        image_hash = hashlib.sha256((self.root / "data" / "assets" / "paper-1" / "fig-1.png").read_bytes()).hexdigest()
        paper["article"]["figures"][0]["sha256"] = image_hash
        paper["mappingReview"] = {"approved": True, "figureId": "fig-1", "figureSHA256": image_hash, "reviewer": "private reviewer", "reviewedAt": "2026-10-11T00:00:00Z", "inputSchemaVerified": True, "commit": "a" * 40, "scriptPath": "plot.py", "scriptSHA256": "b" * 64, "inputPath": "input.csv", "inputSHA256": "c" * 64}
        output = build_site(self.root, catalog, [])
        self.assertEqual(json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["automation"]["status"], "ready")
        source = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        original = source.read_bytes()
        source.write_bytes(original + b"tampered local asset")
        build_site(self.root, catalog, [])
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]
        self.assertEqual(projected["automation"]["status"], "partial")
        self.assertEqual(projected["article"]["figures"][0]["reuseStatus"], "asset_integrity_failed")
        source.write_bytes(original)
        source.unlink()
        build_site(self.root, catalog, [])
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]
        self.assertEqual(projected["automation"]["status"], "partial")
        self.assertTrue(projected["automation"]["warnings"])
        self.assertNotIn("mappingReview", projected)
        self.assertEqual(catalog["papers"][0]["automation"]["status"], "ready", "No source catalogue mutation.")

    def test_tampered_asset_digest_is_not_published_and_old_output_removed(self):
        catalog = sample()
        source = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        figure = catalog["papers"][0]["article"]["figures"][0]
        figure["sha256"] = expected
        output = build_site(self.root, catalog, [])
        self.assertTrue((output / figure["asset"]).is_file())
        source.write_bytes(source.read_bytes() + b"changed local image content")
        build_site(self.root, catalog, [])
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["article"]["figures"][0]
        self.assertEqual(projected["reuseStatus"], "asset_integrity_failed")
        self.assertNotIn("asset", projected)
        self.assertFalse((output / figure["asset"]).exists())
        self.assertEqual(figure["sha256"], expected, "Source hash must never be replaced by local edited bytes.")

    def test_derivative_asset_uses_derivative_hash_preserving_original_source_hash(self):
        catalog = sample()
        source = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        figure = catalog["papers"][0]["article"]["figures"][0]
        figure.update(sha256="a" * 64, assetSHA256=hashlib.sha256(source.read_bytes()).hexdigest(), modification="TIFF converted to PNG; whole image retained")
        output = build_site(self.root, catalog, [])
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["article"]["figures"][0]
        self.assertEqual(projected["reuseStatus"], "allowed")
        self.assertTrue((output / figure["asset"]).is_file())
        self.assertEqual(projected["sha256"], "a" * 64)
        self.assertNotEqual(projected["assetSHA256"], projected["sha256"])
        figure["assetSHA256"] = ""
        build_site(self.root, catalog, [])
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["article"]["figures"][0]
        self.assertEqual(projected["reuseStatus"], "asset_integrity_failed")

    def test_invalid_provided_asset_hash_fails_closed(self):
        for digest in (None, "", "not-a-hash", "a" * 63, "a" * 65, 123):
            catalog = sample()
            catalog["papers"][0]["article"]["figures"][0]["sha256"] = digest
            output = build_site(self.root, catalog, [])
            projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["article"]["figures"][0]
            self.assertEqual(projected["reuseStatus"], "asset_integrity_failed")

    def test_build_downgrades_ready_with_invalid_current_stage(self):
        for stage, status in (("metadata", "metadata_unavailable"), ("repository", "unavailable"), ("ai", "stale")):
            catalog = sample()
            paper = catalog["papers"][0]
            paper["automation"] = {"status": "ready", "stages": {"metadata": "matched", "article": "inspected", "repository": "inspected", "ai": "generated"}}
            paper["automation"]["stages"][stage] = status
            paper["repository"]["status"] = "inspected"
            paper["ai"]["status"] = "generated"
            output = build_site(self.root, catalog, [])
            projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]
            self.assertEqual(projected["automation"]["status"], "partial")

    def test_suggestions_scores_declaration_links_and_report_stages_are_public_not_raw_text(self):
        catalog = sample()
        paper = catalog["papers"][0]
        paper["suggestedAnalysisFunctions"] = ["prediction"]
        paper["suggestedGraphForms"] = [{"id": "heatmap", "label": "热图", "status": "candidate"}]
        paper["selectionScore"] = {"total": 50, "components": {"usability": 25}, "meaning": "选文偏好"}
        paper["article"]["codeURLEvidence"] = [{"url": "https://github.com/author/repo", "sourceURL": "https://example.org/article.xml", "scope": "author_code_availability", "status": "source_declared", "text": "must-not-publish"}]
        report = {"startedAt": "2026-10-11T00:00:00Z", "stageCounts": {"ai": {"missing_key": 1}}, "missingKeys": ["LLM_API_KEY"]}
        output = build_site(self.root, catalog, [report])
        projected = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]
        self.assertEqual(projected["selectionScore"]["total"], 50)
        self.assertEqual(projected["suggestedAnalysisFunctions"], ["prediction"])
        self.assertEqual(projected["analysisFunctions"], ["association"], "Proposal must not alter approved tags.")
        self.assertNotIn("text", projected["article"]["codeURLEvidence"][0])
        self.assertEqual(json.loads((output / "data" / "updates.json").read_text())[0], report)

    def test_keeps_newest_ninety_reports_for_prepend_and_append_history(self):
        reports = [{"startedAt": f"2026-10-{day:02d}T00:00:{second:02d}Z", "newCount": 0} for day in range(1,5) for second in range(30)]
        for ordered in (reports, list(reversed(reports))):
            output = build_site(self.root, sample(), ordered)
            public = json.loads((output / "data" / "updates.json").read_text())
            self.assertEqual(len(public), 90)
            self.assertEqual(public[0]["startedAt"], "2026-10-04T00:00:29Z")
            self.assertEqual(public[-1]["startedAt"], "2026-10-02T00:00:00Z")

    def test_rejects_unsafe_assets_symlinks_and_fake_image_types(self):
        for path in ("assets/../secret.png", "assets/%2e%2e/secret.png", "assets//secret.png", "//server/image.png", "https://example.org/image.png", "assets/image.svg"):
            catalog = sample()
            catalog["papers"][0]["article"]["figures"][0]["asset"] = path
            output = build_site(self.root, catalog, [])
            figure = json.loads((output / "data" / "catalog.json").read_text())["papers"][0]["article"]["figures"][0]
            self.assertNotIn("asset", figure)
        file = self.root / "data" / "assets" / "paper-1" / "fig-1.png"
        file.write_text("<html>not a PNG</html>")
        output = build_site(self.root, sample(), [])
        self.assertEqual(json.loads((output / "data" / "assets-manifest.json").read_text()), [])

    def test_refuses_execution_claims_duplicates_and_nan_before_build(self):
        for mutation in (lambda c: c.update(authorCodeExecuted=True), lambda c: c["papers"].append(copy.deepcopy(c["papers"][0])), lambda c: c["papers"][0].update(runPerformed=True), lambda c: c["papers"][0].update(year=float("nan"))):
            catalog = sample()
            mutation(catalog)
            with self.assertRaises(ValueError):
                build_site(self.root, catalog, [])

    def test_refuses_output_symlinks(self):
        (self.root / "site").symlink_to(self.root / "web", target_is_directory=True)
        with self.assertRaises(ValueError):
            build_site(self.root, sample(), [])

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable: Python build tests still run.")
    def test_js_syntax_and_security_and_many_to_many_filters(self):
        subprocess.run([NODE, "--check", str(PROJECT / "web" / "app.js")], check=True, capture_output=True)
        test_code = r'''
const assert = require('assert');
const ui = require(process.argv[1]);
for (const url of ['javascript:alert(1)', 'http://example.org', 'https://user:pass@example.org', 'https://example.org/?api_key=private', 'https://example.org:8080']) assert.equal(ui.safeURL(url), '');
assert.equal(ui.safeURL('https://example.org/paper#fig1'), 'https://example.org/paper#fig1');
for (const path of ['assets/../secret.png','assets/%2e%2e/secret.png','assets//x.png','https://example.org/x.png','assets/./x.png']) assert.equal(ui.safeAsset(path), '');
assert.equal(ui.safeAsset('assets/paper-1/fig-1.png'), 'assets/paper-1/fig-1.png');
const p = {graphForms:['scatter','heatmap'],formClassificationStatus:'approved',analysisFunctions:['association','integration'],functionClassificationStatus:'approved'};
assert(ui.matchAxis(p,'forms','scatter') && ui.matchAxis(p,'functions','integration'));
assert(!ui.matchAxis(p,'functions','prediction'));
assert.deepEqual(ui.approvedTags({...p,formClassificationStatus:'pending'},'forms'), []);
assert(ui.matchAxis({...p,formClassificationStatus:'pending'},'forms','__pending'));
assert.equal(ui.citationCount({}),null);
assert.equal(ui.citationCount({citation_count:0}),0);
assert(ui.metricLabel({metrics:{if:'5.0',jcr:'Q1'}}).includes('接口未提供'));
assert(ui.metricLabel({metrics:{if:'5.0',metric_year:'unknown'}}).includes('接口未提供'));
assert.equal(ui.safeFigures({article:{figures:[{reuseStatus:'link_only',asset:'assets/a.png'}]}}).length,0);
assert.equal(ui.safeFigures({article:{figures:[{reuseStatus:'allowed',asset:'assets/../a.png'}]}}).length,0);
assert.equal(ui.statusOf({}), 'seed');
assert.equal(ui.statusOf({automation:{status:'excluded'}}), 'excluded');
const sections=ui.inputSections({files:[{name:'selected.csv'}],automaticInputs:{files:[{name:'unrelated.csv'}]}},[{name:'fallback.csv'}]);
assert.equal(sections.selected[0].name,'selected.csv');
assert.equal(sections.automatic[0].name,'unrelated.csv');
assert(ui.aiStateLabel({ai:{status:'stale',summary_zh:'旧说明'}}).includes('来源已更新'));
assert(ui.aiStateLabel({ai:{status:'generated',review_required:true}}).includes('待核'));
const pending={graphForms:['suggested-but-unverified'],formClassificationStatus:'pending',analysisFunctions:[],functionClassificationStatus:'pending'};
const counts=ui.facetCounts([p,pending],'forms');
assert.equal(counts.total,2);assert.equal(counts.pending,1);assert.equal(counts.counts.get('scatter'),1);
assert(!counts.counts.has('suggested-but-unverified'));
const related=ui.facetCounts([p,pending],'forms','association');
assert.equal(related.total,1);assert.equal(related.pending,0);
assert.equal(ui.figureNumber('Figure 1c'),1);
assert.equal(ui.figureNumber('Fig. 12a'),12);
const unselected={label:'Figure 2',reuseStatus:'allowed',asset:'assets/fig2.png'};
const correct={label:'Figure 1',reuseStatus:'allowed',asset:'assets/fig1.png'};
assert.equal(ui.cardFigure({figure:'Figure 1c',article:{figures:[unselected]}}),null);
assert.equal(ui.cardFigure({figure:'Figure 1c',article:{figures:[unselected,correct]}}),correct);
assert.equal(ui.cardFigure({figure:'Figure 1c',article:{figures:[{...correct,label:'Figure 10'}]}}),null);
assert.equal(ui.figureNumber('Figure 1c（CD4 T-cell子面板）'),1);
assert.equal(ui.cardFigure({figure:'Figure 1c（CD4 T-cell子面板）',article:{figures:[unselected]}}),null);
assert.equal(ui.cardFigure({figure:'Figure 1c（CD4 T-cell子面板）',article:{figures:[unselected,correct]}}),correct);
assert.deepEqual(ui.figureReference('Extended Data Fig. 6a'),{scope:'extended',number:'6',panel:'a'});
assert.deepEqual(ui.figureReference('Supplementary Fig. S6'),{scope:'supplementary',number:'6',panel:''});
const main6={label:'Figure 6',reuseStatus:'allowed',asset:'assets/main6.png'}, extended6={label:'Extended Data Fig. 6',reuseStatus:'allowed',asset:'assets/ext6.png'};
assert.equal(ui.cardFigure({figure:'Extended Data Fig. 6a',article:{figures:[main6]}}),null);
assert.equal(ui.cardFigure({figure:'Figure 6a',article:{figures:[extended6]}}),null);
assert.equal(ui.cardFigure({figure:'Extended Data Fig. 6a',article:{figures:[main6,extended6]}}),extended6);
assert.equal(ui.cardFigure({figure:'Figure one（编号解析失败）',article:{figures:[main6]}}),null);
assert.equal(ui.cardFigure({figure:'Figure 6',article:{figures:[{...main6,reference:{scope:'supplementary',number:'6',panel:''}}]}}),null);
assert(ui.metricLabel({metrics:{if:'5.2',retained_field_asof:{if:'2025-01-01T00:00:00Z'}}}).includes('沿用 2025-01-01'));
assert.equal(ui.viewFromHash('#gallery','papers'),'gallery');
assert.equal(ui.viewFromHash('#updates','gallery'),'updates');
assert.equal(ui.viewFromHash('#main','papers'),'papers');
'''
        subprocess.run([NODE, "-e", test_code, str(PROJECT / "web" / "app.js")], check=True, capture_output=True)
        source = (PROJECT / "web" / "app.js").read_text()
        self.assertNotIn("innerHTML", source)
        self.assertNotIn("window.openai", source)
        html = (PROJECT / "web" / "index.html").read_text()
        self.assertIn('<div id="content"></div>', html)
        self.assertIn('<span id="result-count" aria-live="polite">', html)

    def test_large_figure_responsive_layout_rules(self):
        css = (PROJECT / "web" / "styles.css").read_text()
        self.assertIn("max-width:1660px", css)
        self.assertIn("repeat(2,minmax(360px,1fr))", css)
        self.assertNotIn("repeat(3,", css)
        self.assertNotIn("repeat(4,", css)
        self.assertIn("@media(max-width:820px){.gallery{grid-template-columns:1fr}", css)
        self.assertIn("width:min(1500px,92vw)", css)
        self.assertIn(".figure-preview.has-image{aspect-ratio:auto", css)
        self.assertIn(".figure-zoom{position:static", css)
        self.assertIn(".figure-actions .image-status{position:static", css)
        self.assertIn(".full-image-stage.native-size img{max-width:none;max-height:none", css)
        # Check every declaration line, not just overall balance: two malformed
        # media rules can have mutually cancelling parenthesis errors.
        for line in css.splitlines():
            self.assertEqual(line.count("("), line.count(")"), line)
        self.assertEqual(css.count("{"), css.count("}"))


if __name__ == "__main__":
    unittest.main()
