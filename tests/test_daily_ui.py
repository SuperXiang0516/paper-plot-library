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


if __name__ == "__main__":
    unittest.main()
