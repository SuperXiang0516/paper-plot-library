"""Controlled daily success counters are distinct from five candidate attempts."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from paper_plot_library.cloud_update import DAILY_SESSION_COUNTERS, DAILY_SESSION_REASONS
from paper_plot_library.site import public_catalog


PROJECT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "/Users/xiangzhoufu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"


def daily():
    return dict(date="2026-10-11", status="paused", endReason="time_limit_reached",
        batchCount=3, requests=290, aiAttempts=9, uncertainRequests=150, uncertainAiAttempts=15,
        dailyNewCount=2, maxBatches=12, maxRequests=1800, maxAiAttempts=165,
        targetCount=5, inFlightCount=0, localCheckpointSaved=True)


class DailyUiTests(unittest.TestCase):
    def test_public_catalog_strips_private_daily_payload_and_rejects_invalid_counts(self):
        catalog = {"authorCodeExecuted": False, "papers": [], "inputSpecs": {},
            "dailySession": dict(daily(), raw_response="private", api_key="private", inFlight={"reserved": "private"})}
        self.assertEqual(public_catalog(catalog)["dailySession"], daily())
        for key, value in (("dailyNewCount", 6), ("requests", True), ("endReason", "private-provider-error"),
                           ("date", ["2026-10-11"]), ("date", "2026-02-31")):
            with self.subTest(key=key, value=value):
                catalog["dailySession"] = dict(daily(), **{key: value})
                self.assertNotIn("dailySession", public_catalog(catalog))

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable")
    def test_timestamp_validation_matches_public_python_projection(self):
        cases = [dict(daily(), **fields) for fields in (
            {"startedAt": "2026-10-11T12:00:00Z", "updatedAt": "2026-10-11T12:01:00+00:00"},
            {"startedAt": "2026-10-11T12:00:00+00:00", "updatedAt": "2026-10-11T12:01:00Z"},
            {"startedAt": "2026-10-10T12:00:00Z"},
            {"startedAt": "2026-10-11T12:00:00Z", "updatedAt": "2026-10-11T11:59:59+00:00"},
            {"updatedAt": "2026-10-11T12:01:00.123Z"},
            {"updatedAt": "2026-10-11T12:01:00+08:00"},
            {"updatedAt": "2026-02-31T12:01:00Z"},
        )]
        expected = []
        for case in cases:
            catalog = {"authorCodeExecuted": False, "papers": [], "inputSpecs": {}, "dailySession": case}
            expected.append(public_catalog(catalog).get("dailySession"))
        script = "const assert=require('assert'),ui=require(process.argv[1]);assert.deepEqual(JSON.parse(process.argv[2]).map(ui.dailySummary),JSON.parse(process.argv[3]));"
        subprocess.run([NODE, "-e", script, str(PROJECT / "web" / "app.js"), json.dumps(cases), json.dumps(expected)],
                       check=True, capture_output=True)

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable")
    def test_js_daily_goal_validation_and_controlled_enum_parity(self):
        script = r'''
const assert=require('assert'),ui=require(process.argv[1]),data=JSON.parse(process.argv[2]);
assert.deepEqual(ui.dailySummary(data),data);
const lines=ui.dailyLines(data).join('\n');
assert(lines.includes('成功收录 2 / 5 篇') && lines.includes('已处理 3 批'));
assert(lines.includes('不足 5 篇') && lines.includes('重跑不重置'));
assert(ui.dailyLines({...data,status:'finished',endReason:'target_reached',dailyNewCount:5})[1].includes('目标已达到'));
for(const [key,value]of [['dailyNewCount',6],['requests',true],['date',['2026-10-11']],['date','2026-02-31'],['date','0000-01-01'],['endReason','private-response'],['maxRequests',1801]])assert.equal(ui.dailySummary({...data,[key]:value}),null);
assert.equal(ui.dailySummary({...data,status:'finished',endReason:'target_reached',dailyNewCount:2}),null);
assert.equal(ui.dailySummary({...data,status:'active'}),null);
assert.equal(ui.dailySummary({...data,uncertainRequests:1,uncertainAiAttempts:2}),null);
assert.deepEqual(ui.dailySummary({...data,api_key:'private',raw_response:'private'}),data);
assert.deepEqual(Object.keys(ui.DAILY_BOUNDS).sort(),JSON.parse(process.argv[3]).sort());
assert.deepEqual(Object.keys(ui.DAILY_END_LABELS).sort(),JSON.parse(process.argv[4]).filter(Boolean).sort());
assert(ui.updateBudgetLines({rejectedCandidateCount:3,removedCount:1}).join('\n').includes('不占今日成功收录名额'));
assert(ui.reasonLabel('session_time_exhausted').includes('安全运行时间'));
'''
        subprocess.run([NODE, "-e", script, str(PROJECT / "web" / "app.js"), json.dumps(daily()),
            json.dumps(sorted(DAILY_SESSION_COUNTERS)), json.dumps(sorted(DAILY_SESSION_REASONS))],
            check=True, capture_output=True)

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable")
    def test_homepage_daily_counter_is_day_scoped_and_not_a_deployment_or_repair_counter(self):
        script = r'''
const assert=require('assert'),ui=require(process.argv[1]),data=JSON.parse(process.argv[2]);
const now=new Date('2026-10-11T23:59:59Z');
let result=ui.dailyOverview({dailySession:data},[],now);
assert.equal(result.count,2);assert.equal(result.target,5);assert.equal(result.state,'shortfall');
assert(result.status.includes('未达标') && result.reason.includes('时间上限'));
// Yesterday's completed target must never make today's card green.
const reached={...data,status:'finished',endReason:'target_reached',dailyNewCount:5};
result=ui.dailyOverview({dailySession:reached},[],new Date('2026-10-12T00:00:00Z'));
assert.equal(result.count,0);assert.equal(result.date,'2026-10-12');assert.equal(result.state,'not_run');
result=ui.dailyOverview({dailySession:reached},[],now);
assert.equal(result.state,'reached');assert.equal(result.count,5);
// Redeploys, old-paper backfill, unchecked newCount, and synthetic records are not new admissions.
const ignored=[{mode:'backfill',finishedAt:'2026-10-11T18:00:00Z',newCount:5,repairCount:5},
 {mode:'demo',finishedAt:'2026-10-11T19:00:00Z',dailySession:reached},
 {mode:'daily',synthetic:true,finishedAt:'2026-10-11T20:00:00Z',dailySession:reached}];
result=ui.dailyOverview({generatedAt:'2026-10-11T23:59:58Z',bootstrap:{completeCount:55},papers:new Array(55)},ignored,now);
assert.equal(result.count,0);assert.equal(result.state,'not_run');assert.equal(result.latestCollectionAt,'');
result=ui.dailyOverview({synthetic:true,dailySession:reached},[],now);
assert.equal(result.count,null);assert.equal(result.state,'unverified');assert(result.status.includes('模拟'));
result=ui.dailyOverview({},[{mode:'daily',finishedAt:'2026-10-11T20:00:00Z',newCount:5}],now);
assert.equal(result.count,null);assert.equal(result.state,'unverified');
assert.equal(result.latestCollectionAt,'2026-10-11T20:00:00Z');
// A later safe daily checkpoint can supersede stale catalog data.
const latest={...reached,startedAt:'2026-10-11T20:00:00Z',updatedAt:'2026-10-11T20:10:00Z'};
result=ui.dailyOverview({dailySession:{...data,updatedAt:'2026-10-11T19:00:00Z'}},
 [{mode:'daily',finishedAt:'2026-10-11T20:09:00Z',dailySession:latest}],now);
assert.equal(result.count,5);assert.equal(result.snapshotAt,'2026-10-11T20:10:00Z');
assert.equal(result.latestCollectionAt,'2026-10-11T20:09:00Z');
for(const reason of ['ai_configuration_blocked','http_budget_exhausted','checkpoint_callback_failed','no_progress']){
 result=ui.dailyOverview({dailySession:{...data,endReason:reason}},[],now);
 assert.equal(result.state,'shortfall');assert.equal(result.reason,ui.DAILY_END_LABELS[reason]);
}
// The banner only contains typed summaries; private queue details cannot escape via a report.
result=ui.dailyOverview({dailySession:{...data,raw_response:'secret-private',doi:'10.1000/private'}},
 [{mode:'backfill',finishedAt:'2026-10-11T18:00:00Z',doi:'10.1000/private',error:'secret-private'}],now);
assert(!JSON.stringify(result).includes('secret-private') && !JSON.stringify(result).includes('10.1000/private'));
assert.equal(ui.dailyOverview({},[],new Date('invalid')),null);
// Future timestamps cannot make a current UTC day appear complete, including old summaries lacking updatedAt.
for(const fields of [{startedAt:'2026-10-11T23:59:59Z'},{updatedAt:'2026-10-11T23:59:59Z'}]){
 result=ui.dailyOverview({dailySession:{...reached,...fields}},[],new Date('2026-10-11T23:00:00Z'));
 assert.equal(result.count,0);assert.equal(result.state,'not_run');
}
result=ui.dailyOverview({},[{mode:'daily',startedAt:'2026-10-11T23:59:59+00:00',dailySession:reached}],new Date('2026-10-11T23:00:00Z'));
assert.equal(result.count,0);assert.equal(result.state,'not_run');
result=ui.dailyOverview({dailySession:{...data,updatedAt:'2026-10-11T12:00:00+00:00'}},[
 {mode:'daily',finishedAt:'2026-10-11T12:00:01Z',dailySession:{...reached,updatedAt:'2026-10-11T12:00:01Z'}}],now);
assert.equal(result.count,5);assert.equal(result.state,'reached');
'''
        subprocess.run([NODE, "-e", script, str(PROJECT / "web" / "app.js"), json.dumps(daily())],
                       check=True, capture_output=True)

    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable")
    def test_daily_timestamps_are_safe_and_beijing_day_boundary_is_explicit(self):
        script = r'''
const assert=require('assert'),ui=require(process.argv[1]),data=JSON.parse(process.argv[2]);
for(const value of ['2026-02-31T12:00:00Z','2026-10-11T25:00:00Z','0000-01-01T00:00:00Z','2026-10-11',{},'secret-private']){
 assert.equal(ui.dailyTimestamp(value),'');assert.equal(ui.dailySummary({...data,updatedAt:value}),null);
}
assert.equal(ui.beijingTimestamp('2026-10-11T23:59:59Z'),'2026-10-12 07:59:59（北京时间）');
assert.equal(ui.beijingTimestamp('2026-10-12T00:00:00Z'),'2026-10-12 08:00:00（北京时间）');
const stamped={...data,startedAt:'2026-10-11T12:00:00Z',updatedAt:'2026-10-11T12:01:00Z'};
assert.deepEqual(ui.dailySummary(stamped),stamped);
const phase=ui.bootstrapLines({status:'needs_attention',totalCount:2,completeCount:0,pendingCount:1,attentionCount:1,issues:[]}).join('\n');
assert(phase.includes('不阻塞每日新增') && !phase.includes('暂停新') && !phase.includes('才进入'));
'''
        subprocess.run([NODE, "-e", script, str(PROJECT / "web" / "app.js"), json.dumps(daily())],
                       check=True, capture_output=True)
        html = (PROJECT / "web" / "index.html").read_text()
        script_text = (PROJECT / "web" / "app.js").read_text()
        self.assertIn('id="daily-overview"', html)
        self.assertIn('aria-labelledby="daily-overview-title"', html)
        self.assertLess(html.index('id="daily-overview"'), html.index('id="collection-phase"'))
        self.assertIn("北京时间 08:00 换日", script_text)
        self.assertIn("旧文献修补", script_text)
        self.assertIn("setInterval", script_text, "An open page must stop showing yesterday as today at UTC midnight.")


if __name__ == "__main__":
    unittest.main()
