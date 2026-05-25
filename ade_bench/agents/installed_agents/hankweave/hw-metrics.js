// Read the hankweave execution state.json, sum codon cost/tokens, print one compact JSON line.
// Copied (as a real file, NOT generated) to /installed-agent by HankweaveAgent.perform_task and
// invoked as the last line of run-hankweave.sh; HankweaveAgent._parse_agent_output reads the last
// '{...input_tokens...}' line off stdout. Field names verified against hankweave 0.6.x state.json.
const fs = require("fs");
const path = process.argv[2];
const out = {
  input_tokens: 0,
  output_tokens: 0,
  cache_tokens: 0,
  num_turns: 1,
  runtime_ms: 0,
  cost_usd: 0.0,
};
try {
  const s = JSON.parse(fs.readFileSync(path, "utf8"));
  const runs = s.runs || [];
  const run = runs[runs.length - 1] || {};
  for (const c of run.codons || []) {
    const t = c.finalTokens || c.currentTokens || {};
    out.input_tokens += t.inputTokens || 0;
    out.output_tokens += t.outputTokens || 0;
    out.cache_tokens += (t.cacheCreationTokens || 0) + (t.cacheReadTokens || 0);
    const cost =
      c.finalCost != null ? c.finalCost : c.currentCost != null ? c.currentCost : c.partialCost;
    out.cost_usd += cost || 0;
  }
} catch (e) {
  // No/invalid state.json (e.g. hankweave failed before writing) — still emit a parseable line.
}
console.log(JSON.stringify(out));
