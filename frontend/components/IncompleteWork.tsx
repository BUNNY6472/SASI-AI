import { PartialOutput, providerMeta } from "@/lib/api";
import { AgentChip } from "./ui";

export default function IncompleteWork({
  outputs,
}: {
  outputs: PartialOutput[];
}) {
  return (
    <section className="glass incomplete-work">
      <h2>Incomplete work</h2>
      <p>
        These saved fragments ended before completion. They are unreviewed and
        do not count as completed analyses, accepted tasks or passing checks.
      </p>
      {outputs.map((output) => (
        <details className="partial-output" key={output.id}>
          <summary>
            <span className="partial-summary">
              <span className="badge">Incomplete · unreviewed</span>
              <AgentChip id={output.provider} />
              <span>{output.label || output.kind.replaceAll("_", " ")}</span>
            </span>
          </summary>
          <div className="partial-body">
            <dl>
              <div>
                <dt>Stopped because</dt>
                <dd>{output.reason.replaceAll("_", " ")}</dd>
              </div>
              {output.model && (
                <div>
                  <dt>Model</dt>
                  <dd>{output.model}</dd>
                </div>
              )}
              {output.task_id && (
                <div>
                  <dt>Task</dt>
                  <dd>{output.task_id}</dd>
                </div>
              )}
            </dl>
            {output.usage_estimated && (
              <p className="small">
                Token usage for this fragment was estimated.
              </p>
            )}
            <div
              className="request-text"
              role="region"
              aria-label={`${providerMeta(output.provider).name} incomplete output`}
              tabIndex={0}
            >
              {output.text}
            </div>
          </div>
        </details>
      ))}
    </section>
  );
}
