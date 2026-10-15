"""Offline browser module checks for the three compact research options."""
from pathlib import Path
import shutil
import subprocess
import unittest


PROJECT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or "/Users/xiangzhoufu/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"


class ScopeUiTests(unittest.TestCase):
    @unittest.skipUnless(Path(NODE).exists(), "Node unavailable")
    def test_research_options_labels_and_filters_are_controlled(self):
        script = r'''
const assert=require('assert'),ui=require(process.argv[1]);
const paper=tier=>({researchScope:{policy:'aging-sequencing-metabolic',status:'included',tier}});
for(const tier of ['primary','expansion','tools']){
 assert(ui.scopeTierLabel(paper(tier)).length>0);
 assert(ui.matchesResearchScope(paper(tier),tier));
 assert(ui.matchesResearchScope(paper(tier),''));
}
assert(ui.scopeTierLabel(paper('tools')).includes('通用组学'));
assert(!ui.matchesResearchScope(paper('tools'),'primary'));
assert(!ui.matchesResearchScope(paper('primary'),'tools'));
assert(!ui.matchesResearchScope(paper('tools'),'__proto__'));
assert.equal(ui.scopeTierLabel(paper('__proto__')),'');
assert.equal(ui.scopeTierLabel({researchScope:{...paper('tools').researchScope,status:'excluded'}}),'');
assert.equal(ui.scopeTierLabel({researchScope:{...paper('tools').researchScope,policy:'unknown'}}),'');
assert(ui.matchesResearchScope({},''));
assert(!ui.matchesResearchScope({},'tools'));
'''
        subprocess.run([NODE, "-e", script, str(PROJECT / "web" / "app.js")], check=True, capture_output=True)
        html = (PROJECT / "web" / "index.html").read_text()
        source = (PROJECT / "web" / "app.js").read_text()
        self.assertIn('id="scope-filter"', html)
        self.assertIn('<option value="tools">通用组学 · 方法与工具</option>', html)
        self.assertIn("matchesResearchScope(paper,state.scope)", source)
        self.assertIn("['scope-filter','scope']", source)
        self.assertIn("$('scope-filter').value=''", source)


if __name__ == "__main__":
    unittest.main()
