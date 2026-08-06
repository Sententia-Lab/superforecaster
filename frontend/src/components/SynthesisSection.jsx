import {
  adjustedLensRate,
  lensRate,
  lensesFor,
  pct,
  subClaimRate,
} from "../derive.js";

function ViolationsList({ violations }) {
  if (!violations?.length) return null;
  return (
    <div className="card">
      <div className="card-head">
        <span className="headline">Methodology violations that survived</span>
      </div>
      {violations.map((v, i) => (
        <div key={i} className="violation">
          <span className="p">P{v.principle}</span>
          <span>
            <b>{v.name}</b> — {v.detail}
          </span>
        </div>
      ))}
    </div>
  );
}

/**
 * The arithmetic, then the number, then the story. The table recomputes every rate
 * client-side with `derive.js` (mirroring `checks.py`) from the same payloads the
 * checks ran on — if the picture and the check ever disagree, one of them is lying.
 */
export default function SynthesisSection({ payload, decomposition }) {
  const { outside, inside, forecast, violations } = payload;
  const subs = decomposition?.sub_claims || [];

  return (
    <div>
      <div className="card">
        <div className="card-head">
          <span className="headline">Final probability</span>
        </div>
        <div className="display-number" style={{ margin: "10px 0" }}>
          {pct(forecast.probability, forecast.probability < 0.1 ? 1 : 0)}
        </div>
        <div className="card-sub">
          Anchor {pct(payload.anchor)} → implied {pct(payload.implied)} → stated{" "}
          {pct(forecast.probability)}. The synthesis agent may move the number at
          most ±{(payload.derivation_slack * 100).toFixed(0)} points from implied
          (configurable via CHECK_DERIVATION_SLACK)
          {payload.attempts > 1 ? ` · took ${payload.attempts} attempts` : ""}.
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <span className="headline">How the number was built</span>
        </div>
        <table className="arith">
          <thead>
            <tr>
              <th>Sub-question</th>
              <th>Lens</th>
              <th style={{ textAlign: "right" }}>Counted</th>
              <th style={{ textAlign: "right" }}>Adjusted</th>
              <th style={{ textAlign: "right" }}>Weight</th>
              <th style={{ textAlign: "right" }}>Blended</th>
            </tr>
          </thead>
          <tbody>
            {subs.map((s) => {
              const lenses = lensesFor(s.id, outside);
              if (!lenses.length) {
                return (
                  <tr key={s.id}>
                    <td>{s.id}</td>
                    <td colSpan={4} style={{ color: "var(--pv-text-3)" }}>
                      {s.knowability === "judgment"
                        ? "judgment — estimated"
                        : "no lens landed"}
                    </td>
                    <td className="num">{pct(s.probability)}</td>
                  </tr>
                );
              }
              return lenses.map((l, i) => (
                <tr key={`${s.id}:${l.name}`}>
                  <td>{i === 0 ? s.id : ""}</td>
                  <td>{l.name}</td>
                  <td className="num">{pct(lensRate(l))}</td>
                  <td className="num">{pct(adjustedLensRate(l, inside))}</td>
                  <td className="num">{l.weight.toFixed(2)}</td>
                  <td className="num">
                    {i === 0 ? pct(subClaimRate(s.id, outside, inside)) : ""}
                  </td>
                </tr>
              ));
            })}
          </tbody>
        </table>
        <div className="card-sub" style={{ marginTop: 8 }}>
          Chain rule: <b>{decomposition?.chain_rule}</b> —{" "}
          {decomposition?.chain_note}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <span className="headline">Rationale</span>
        </div>
        <div className="prose" style={{ marginTop: 6 }}>
          {forecast.reasoning}
        </div>
        {forecast.extreme_justification ? (
          <div className="card-sub" style={{ marginTop: 8 }}>
            <b>Extreme justification:</b> {forecast.extreme_justification}
          </div>
        ) : null}
      </div>

      {payload.reflection ? (
        <div className="card">
          <div className="card-head">
            <span className="headline">The case against</span>
          </div>
          <div className="card-sub" style={{ marginTop: 4 }}>
            <b>Steel man:</b> {payload.reflection.steel_man}
          </div>
          <div className="card-sub" style={{ marginTop: 4 }}>
            <b>Would change my mind:</b>{" "}
            {payload.reflection.what_would_change_my_mind}
          </div>
          <div className="src-chips" style={{ marginTop: 8 }}>
            {(payload.reflection.bias_checks || []).map((b, i) => (
              <span key={i} className="src-chip" title={b.assessment}>
                {b.bias}
              </span>
            ))}
          </div>
        </div>
      ) : null}

      <ViolationsList violations={violations} />
    </div>
  );
}
