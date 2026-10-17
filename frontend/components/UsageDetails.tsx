import { RunUsage, TokenUsage, providerMeta } from "@/lib/api";

const number = (value?: number) => Math.max(0, value || 0).toLocaleString("en-IN");
const stageName = (key: string) => key.replace(/[_-]+/g, " ").replace(/^\w/, (letter) => letter.toUpperCase());
const unknownInput = (usage: Partial<TokenUsage>) => Math.max(
  0, (usage.input_tokens || 0) - (usage.uncached_input_tokens || 0)
    - (usage.cache_read_input_tokens || 0) - (usage.cache_write_input_tokens || 0),
);

function BreakdownTable({ rows, label }: {
  rows: [string, Partial<TokenUsage>][];
  label: string;
}) {
  if (!rows.length) return null;
  return (
    <div className="usage-table-scroll" tabIndex={0} role="region" aria-label={label}>
      <table className="usage-table">
        <caption>{label}</caption>
        <thead><tr><th scope="col">{label === "By model" ? "Model" : "Stage"}</th><th scope="col">Calls</th><th scope="col">Input</th><th scope="col">Cache reads</th><th scope="col">Output</th><th scope="col">Estimated*</th></tr></thead>
        <tbody>{rows.map(([name, value]) => (
          <tr key={name}>
            <th scope="row">{name}</th>
            <td>{number(value.calls)}</td>
            <td>{number(value.input_tokens)}</td>
            <td>{value.detailed_calls ? number(value.cache_read_input_tokens) : "—"}</td>
            <td>{number(value.output_tokens)}</td>
            <td>{value.estimated_tokens ? number(value.estimated_tokens) : value.detailed_calls ? "0" : "—"}</td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

export default function UsageDetails({ usage }: { usage: RunUsage }) {
  const detailed = !!usage.usage_details_available;
  const unclassified = usage.unclassified_input_tokens ?? unknownInput(usage);
  const stages: [string, Partial<TokenUsage>][] = Object.entries(usage.by_stage || {}).map(([name, value]) => [stageName(name), value]);
  const providers: [string, Partial<TokenUsage>][] = Object.entries(usage.by_provider).map(([name, value]) => [providerMeta(name).name, value]);
  return (
    <details className="usage-details glass">
      <summary><span>Token usage</span><strong>{number(usage.input_tokens + usage.output_tokens)} <small>tokens · {number(usage.calls)} calls</small></strong></summary>
      <div className="usage-details-body">
        <p>Usage across all calls in this session. Cached tokens count toward the session allowance; these totals are not a billing statement.</p>
        <dl className="usage-counters">
          <div><dt>Total input</dt><dd>{number(usage.input_tokens)}</dd></div>
          <div><dt>Output, including reasoning</dt><dd>{number(usage.output_tokens)}</dd></div>
          {detailed && <>
            <div><dt>Uncached input</dt><dd>{number(usage.uncached_input_tokens)}</dd></div>
            <div><dt>Cache reads</dt><dd>{number(usage.cache_read_input_tokens)}</dd></div>
            <div><dt>Cache writes</dt><dd>{number(usage.cache_write_input_tokens)}</dd></div>
            <div><dt>Reported reasoning*</dt><dd>{number(usage.thinking_tokens)}</dd></div>
            <div><dt>Estimated tokens included*</dt><dd>{number(usage.estimated_tokens)}</dd></div>
            {unclassified > 0 && <div><dt>Input without a breakdown</dt><dd>{number(unclassified)}</dd></div>}
          </>}
        </dl>
        {detailed ? <p className="usage-explainer">Detailed usage was recorded for {number(usage.detailed_calls)} calls. Reasoning is included in output and only separated when reported by the provider. Estimated tokens are already included in the totals; interrupted calls can use conservative estimates.{unclassified > 0 ? " Some input came from calls without a complete cache breakdown." : ""}</p> : <p className="usage-explainer">A cache and reasoning breakdown is not available for this session. The saved input and output totals are shown above.</p>}
        <BreakdownTable rows={stages} label="By stage" />
        <BreakdownTable rows={providers} label="By model" />
      </div>
    </details>
  );
}
