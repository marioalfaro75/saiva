import type { PreviewRow } from "../api/types";
import { matchEvidenceOf, neighbourLine } from "./matchEvidence";

/**
 * The evidence for a possible duplicate, always visible — no toggle, no hover.
 *
 * It used to be a `title=""` on the status pill, which is nothing at all on a phone
 * and nothing to a keyboard. A segment that exists solely to be decided should not
 * hide the thing you decide on behind an interaction, so this is plain text in the
 * flow. Keeping it to a few sentences rather than a comparison grid is what lets it
 * stay always-visible across three hundred rows and still fit a 390px screen.
 *
 * No interactive element, so there is no focus order, no aria-expanded contract and
 * nothing for a sort to desynchronise. One definition, used by both render paths.
 */
export function MatchEvidence({ row }: { row: PreviewRow }) {
  const evidence = matchEvidenceOf(row);
  if (!evidence) return null;

  return (
    <div className="match-evidence">
      <p className="match-evidence-head">{evidence.headline}</p>
      <p>
        <span className="muted">Already in Saiva: </span>
        {evidence.stored}
      </p>
      {evidence.yourFile && (
        <p>
          <span className="muted">Your file says: </span>
          {evidence.yourFile}
        </p>
      )}
      {evidence.provenance && <p className="muted">{evidence.provenance}</p>}
      {evidence.nearby && (
        <>
          <p className="muted">{evidence.nearby.summary}</p>
          {evidence.nearby.rows.length > 0 && (
            <ul className="match-nearby">
              {evidence.nearby.rows.map((n) => (
                <li key={`${n.txn_date}-${n.amount_cents}-${n.description}`}>
                  {neighbourLine(n)}
                </li>
              ))}
              {evidence.nearby.more > 0 && (
                <li className="muted">and {evidence.nearby.more} more</li>
              )}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
