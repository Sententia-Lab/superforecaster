import Accordion from "./Accordion.jsx";
import DependentGroups from "./DependentGroups.jsx";
import Prose from "./Prose.jsx";
import {
  adjustedLensRate,
  adjustmentsForLens,
  lensRate,
  lensesFor,
  pct,
  subQuestionRate,
} from "../derive.js";
import { firstSentence, ordinal, subQuestionLabel } from "../labels.js";

const COLUMNS = 6;

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
            <b>{v.name}</b> — <Prose className="prose tight">{v.detail}</Prose>
          </span>
        </div>
      ))}
    </div>
  );
}

/** `+0.10`, `−0.04`, `±0.00` — the same rendering the modifier cards use. */
function move(a) {
  if (a.is_noise) return "±0.00";
  return `${a.direction === "down" ? "−" : "+"}${a.magnitude.toFixed(2)}`;
}

function LensRows({ lens, index, inside }) {
  return (
    <>
      <tr>
        <td>
          {ordinal("Lens", index)} — {lens.name}
        </td>
        <td colSpan={2} />
        <td className="num">{pct(lensRate(lens))}</td>
        <td className="num">{pct(adjustedLensRate(lens, inside))}</td>
        <td className="num">{lens.weight.toFixed(2)}</td>
      </tr>
      {adjustmentsForLens(lens, inside).map((a, i) => (
        <tr className="mod" key={i}>
          <td />
          <td className="name" colSpan={2}>
            {ordinal("Modifier", i)} — {a.title || firstSentence(a.evidence, 60)}
          </td>
          <td className={`num adj-mag ${a.is_noise ? "noise" : a.direction}`}>
            {move(a)}
          </td>
          <td />
          <td />
        </tr>
      ))}
    </>
  );
}

/**
 * One sub-question: a spanning header carrying the question itself, its lenses, every
 * modifier under its lens, then the blend.
 *
 * The question is a sentence, so it gets a row rather than a column — a column wide
 * enough to hold it would squeeze every number beside it.
 */
function SubQuestionRows({ sub, outside, inside }) {
  const lenses = lensesFor(sub.id, outside);

  const header = (
    <tr className="group">
      <td colSpan={COLUMNS}>
        <span className="label">{subQuestionLabel(sub.id)}</span>
        {sub.question}
      </td>
    </tr>
  );

  if (!lenses.length) {
    return (
      <>
        {header}
        <tr>
          <td colSpan={COLUMNS - 1} style={{ color: "var(--pv-text-3)" }}>
            {sub.knowability === "judgment" ? "judgment — estimated" : "no lens landed"}
          </td>
          <td className="num">{pct(sub.probability)}</td>
        </tr>
      </>
    );
  }

  return (
    <>
      {header}
      {lenses.map((l, i) => (
        <LensRows key={l.name} lens={l} index={i} inside={inside} />
      ))}
      <tr className="total">
        <td colSpan={COLUMNS - 1}>Blended</td>
        <td className="num">{pct(subQuestionRate(sub.id, outside, inside))}</td>
      </tr>
    </>
  );
}

/**
 * The arithmetic, then the number, then the story. Every rate is recomputed client-side
 * by `derive.js` (mirroring `checks.py`) from the same payloads the checks ran on — if
 * the picture and the check ever disagree, one of them is lying.
 *
 * The table carries every lens *and* every modifier, so the whole derivation sits on one
 * screen instead of being spread across the cells that produced it.
 */
export default function SynthesisSection({ payload, decomposition }) {
  const { outside, inside, forecast, violations } = payload;
  const subs = decomposition?.sub_questions || [];

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
          {pct(forecast.probability)}. The synthesis agent may move the number at most ±
          {(payload.derivation_slack * 100).toFixed(0)} points from implied (configurable
          via CHECK_DERIVATION_SLACK)
          {payload.attempts > 1 ? ` · took ${payload.attempts} attempts` : ""}.
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <span className="headline">How the number was built</span>
        </div>
        <div className="arith-scroll">
          <table className="arith">
            <thead>
              <tr>
                <th>Lens</th>
                <th colSpan={2}>Modifier</th>
                <th style={{ textAlign: "right" }}>Counted / move</th>
                <th style={{ textAlign: "right" }}>Adjusted</th>
                <th style={{ textAlign: "right" }}>Weight</th>
              </tr>
            </thead>
            <tbody>
              {subs.map((s) => (
                <SubQuestionRows key={s.id} sub={s} outside={outside} inside={inside} />
              ))}
            </tbody>
          </table>
        </div>
        <div className="card-sub" style={{ marginTop: 8 }}>
          Chain rule: <b>{decomposition?.chain_rule}</b> — {decomposition?.chain_note}
        </div>
        <DependentGroups decomposition={decomposition} />
      </div>

      <div className="card">
        <div className="card-head">
          <span className="headline">Rationale</span>
        </div>
        <Prose>{forecast.reasoning}</Prose>
        {forecast.extreme_justification ? (
          <div className="card-sub" style={{ marginTop: 8 }}>
            <b>Extreme justification:</b>{" "}
            <Prose className="prose tight">{forecast.extreme_justification}</Prose>
          </div>
        ) : null}
      </div>

      {payload.reflection ? (
        <div className="card">
          <div className="card-head">
            <span className="headline">The case against</span>
          </div>
          <Accordion summary={<span className="grow">Steel man</span>}>
            <Prose>{payload.reflection.steel_man}</Prose>
          </Accordion>
          <Accordion summary={<span className="grow">Would change my mind</span>}>
            <Prose>{payload.reflection.what_would_change_my_mind}</Prose>
          </Accordion>
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
